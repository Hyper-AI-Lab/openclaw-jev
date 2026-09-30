"""CRUD helpers for supplementary task messages."""
from __future__ import annotations

import uuid
from datetime import timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.models import Task, TaskMessage
from app.notification_policy import is_internal_task
from app.orchestrator.prompt_policy import USER_TIMEZONE
from app.task_registry.session_identity import dialogue_lookup_keys


async def add_task_message(
    task_id: str,
    content: str,
    *,
    role: str = "user",
    source: str = "api",
    db: Optional[AsyncSession] = None,
    slack_ts: Optional[str] = None,
    kind: str = "request",
    session_key: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    msg_id = str(uuid.uuid4())
    row = TaskMessage(
        id=msg_id,
        task_id=task_id,
        role=role,
        content=text[:8000],
        source=source,
        slack_ts=slack_ts or None,
        kind=kind,
        session_key=session_key or None,
        meta={k: v for k, v in (meta or {}).items() if v not in (None, "", [], {})} or None,
    )

    async def _commit(session: AsyncSession) -> None:
        session.add(row)
        await session.commit()

    if db is not None:
        db.add(row)
        await db.flush()
        return msg_id
    async with AsyncSessionLocal() as session:
        await _commit(session)
    return msg_id


async def list_task_messages(
    task_id: str,
    *,
    limit: int = 20,
    db: Optional[AsyncSession] = None,
) -> List[TaskMessage]:
    async def _query(session: AsyncSession) -> List[TaskMessage]:
        result = await session.execute(
            select(TaskMessage)
            .where(TaskMessage.task_id == task_id)
            .order_by(TaskMessage.created_at.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))

    if db is not None:
        return await _query(db)
    async with AsyncSessionLocal() as session:
        return await _query(session)


def format_session_dialogue(turns: List[Dict[str, Any]]) -> str:
    """Build a RECENT DIALOGUE block from prior same-session Slack turns."""
    lines: List[str] = []
    for turn in turns:
        role = str(turn.get("role") or "user").strip().lower()
        who = "Kirill" if role == "user" else "Aura"
        body = " ".join(str(turn.get("content") or "").split())[:400]
        if not body:
            continue
        when = turn.get("created_at")
        stamp = ""
        if when is not None:
            try:
                dt = when
                if getattr(dt, "tzinfo", None) is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                stamp = dt.astimezone(ZoneInfo(USER_TIMEZONE)).strftime("%H:%M")
            except Exception:
                stamp = str(when)[:16]
        prefix = f"[{stamp}] " if stamp else ""
        lines.append(f"{prefix}{who}: {body}")
    if not lines:
        return ""
    return (
        "RECENT DIALOGUE (same Slack session — continue this conversation):\n"
        + "\n".join(lines)
    )


async def recent_session_dialogue_block(
    session_key: str,
    *,
    exclude_task_id: Optional[str] = None,
    limit_tasks: int = 4,
    db: Optional[AsyncSession] = None,
) -> str:
    """Prior user/assistant Slack turns on this session (not process-scoped memory)."""
    if not session_key:
        return ""

    async def _query(session: AsyncSession) -> str:
        lookup = dialogue_lookup_keys(session_key)
        if not lookup:
            return ""
        q = (
            select(Task)
            .where(Task.openclaw_session_key.in_(lookup))
            .where(Task.task_type == "user")
            .order_by(Task.created_at.desc())
            .limit(max(limit_tasks + 2, 6))
        )
        result = await session.execute(q)
        tasks = []
        for t in result.scalars().all():
            if exclude_task_id and t.id == exclude_task_id:
                continue
            if is_internal_task(t.goal or "", t.task_type or "", []):
                continue
            tasks.append(t)
            if len(tasks) >= limit_tasks:
                break
        tasks.reverse()
        if not tasks:
            return ""
        ids = [t.id for t in tasks]
        msg_q = (
            select(TaskMessage)
            .where(TaskMessage.task_id.in_(ids))
            .where(TaskMessage.role.in_(("user", "assistant")))
            .order_by(TaskMessage.created_at.asc())
        )
        msg_rows = (await session.execute(msg_q)).scalars().all()
        turns = [
            {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at,
            }
            for m in msg_rows
        ]
        return format_session_dialogue(turns)

    if db is not None:
        return await _query(db)
    async with AsyncSessionLocal() as session:
        return await _query(session)


async def task_for_slack_message(ts: str) -> Optional[Dict[str, Any]]:
    """The task a Slack message belongs to: one of Kirill's messages or a part of Aura's reply."""
    from app.db.models import SideEffectReceipt

    if not ts:
        return None
    async with AsyncSessionLocal() as session:
        task_id = (
            await session.execute(
                select(TaskMessage.task_id).where(TaskMessage.slack_ts == ts).limit(1)
            )
        ).scalar_one_or_none()
        if not task_id:
            receipt = (
                await session.execute(
                    select(SideEffectReceipt.metadata_ref)
                    .where(SideEffectReceipt.metadata_ref["ts"].as_string() == ts)
                    .limit(1)
                )
            ).scalar_one_or_none()
            task_id = (receipt or {}).get("task_id")
        if not task_id:
            return None
        task = await session.get(Task, task_id)
    if task is None:
        return None
    return {"task_id": task.id, "status": task.status, "goal": (task.goal or "")[:300]}
