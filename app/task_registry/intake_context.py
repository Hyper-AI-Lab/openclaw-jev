"""Build intake context from active tasks, registry history, and messages."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_task_registry_config
from app.task_registry.messages import list_task_messages, recent_session_dialogue_block
from app.task_registry.retriever import hybrid_search_bounded

logger = logging.getLogger("rmp.intake_context")


async def _dialogue_lines(session_key: str) -> List[str]:
    """The conversation before this message, one line per turn, for Jev and the analyst."""
    try:
        block = await recent_session_dialogue_block(session_key)
    except Exception as exc:
        logger.warning("Intake dialogue unavailable: %s", type(exc).__name__)
        return []
    return block.splitlines()[1:] if block else []


async def _load_supplementary_messages(
    task_ids: List[str],
) -> Dict[str, List[Dict[str, str]]]:
    if not task_ids:
        return {}

    async def _one(tid: str) -> Tuple[str, List[Dict[str, str]]]:
        msgs = await list_task_messages(tid, limit=5)
        if not msgs:
            return tid, []
        return tid, [
            {"role": m.role, "content": m.content[:500], "source": m.source}
            for m in msgs
        ]

    pairs = await asyncio.gather(*[_one(tid) for tid in task_ids])
    return {tid: rows for tid, rows in pairs if rows}


async def _replied_to(reply_to: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The earlier Slack message this one replies to, and the task it belongs to."""
    from app.task_registry.messages import task_for_slack_message

    if not reply_to or not reply_to.get("id"):
        return None
    hit = await task_for_slack_message(str(reply_to["id"]))
    return {"quoted": str(reply_to.get("body") or "")[:600], **(hit or {})}


async def _registry_row(task_id: str) -> Optional[Dict[str, Any]]:
    from sqlalchemy import select

    from app.db.database import AsyncSessionLocal
    from app.db.models import TaskRegistryEntry

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(TaskRegistryEntry).where(TaskRegistryEntry.task_id == task_id))
        ).scalars().first()
    if row is None:
        return None
    return {
        "task_id": row.task_id,
        "terminal_status": row.terminal_status,
        "process_type": row.process_type,
        "intent_snippet": row.intent_snippet,
        "outcome_summary": row.outcome_summary,
        "task_ended_at": row.task_ended_at.isoformat() if row.task_ended_at else None,
    }


async def assemble_intake_context(
    intent: str,
    *,
    session_key: str = "",
    recurrence_key: Optional[str] = None,
    tags: Optional[List[str]] = None,
    reply_to: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = get_task_registry_config()
    deadline = float(cfg.get("intake_vector_deadline_sec", 10))
    retrieval, dialogue = await asyncio.gather(
        hybrid_search_bounded(
            intent,
            session_key=session_key or None,
            recurrence_key=recurrence_key,
            limit=5,
            deadline_sec=deadline,
        ),
        _dialogue_lines(session_key),
    )
    from app.task_registry.intake_decision_engine import user_visible_active_tasks

    retrieval["active_tasks"] = user_visible_active_tasks(
        retrieval.get("active_tasks") or [],
        tags=tags,
    )
    replied = await _replied_to(reply_to)
    known = {t.get("task_id") for t in retrieval["active_tasks"] + (retrieval.get("recent_registry") or [])}
    if replied and replied.get("task_id") and replied["task_id"] not in known:
        row = await _registry_row(replied["task_id"])
        if row:
            retrieval["recent_registry"] = [row, *(retrieval.get("recent_registry") or [])]
    task_ids: List[str] = []
    for bucket in ("active_tasks", "recent_registry", "vector_similar", "evidence_pack"):
        for item in retrieval.get(bucket, []):
            tid = item.get("task_id")
            if not tid or tid in task_ids:
                continue
            task_ids.append(tid)
            if len(task_ids) >= 3:
                break
        if len(task_ids) >= 3:
            break
    supplementary = await _load_supplementary_messages(task_ids)
    return {
        "intent": intent[:20000],
        "session_key": session_key,
        "recurrence_key": recurrence_key,
        "tags": tags or [],
        "recent_dialogue": dialogue,
        "active_tasks": retrieval["active_tasks"],
        "recent_registry": retrieval["recent_registry"],
        "vector_similar": retrieval["vector_similar"],
        "evidence_pack": retrieval.get("evidence_pack") or [],
        "memory_hits": retrieval.get("memory_hits") or [],
        "fts_hits": retrieval.get("fts_hits") or [],
        "supplementary_messages": supplementary,
        "reply_to": replied,
    }
