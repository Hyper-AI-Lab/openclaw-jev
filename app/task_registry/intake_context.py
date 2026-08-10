"""Build intake context from active tasks, registry history, and messages."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from app.config import get_task_registry_config
from app.task_registry.messages import list_task_messages
from app.task_registry.retriever import hybrid_search_bounded


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


async def assemble_intake_context(
    intent: str,
    *,
    session_key: str = "",
    recurrence_key: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    cfg = get_task_registry_config()
    deadline = float(cfg.get("intake_vector_deadline_sec", 10))
    retrieval = await hybrid_search_bounded(
        intent,
        session_key=session_key or None,
        recurrence_key=recurrence_key,
        limit=5,
        deadline_sec=deadline,
    )
    task_ids: List[str] = []
    for bucket in ("active_tasks", "recent_registry", "vector_similar"):
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
        "intent": intent[:2000],
        "session_key": session_key,
        "recurrence_key": recurrence_key,
        "tags": tags or [],
        "active_tasks": retrieval["active_tasks"],
        "recent_registry": retrieval["recent_registry"],
        "vector_similar": retrieval["vector_similar"],
        "supplementary_messages": supplementary,
    }
