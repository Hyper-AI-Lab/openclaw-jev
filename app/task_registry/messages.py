"""CRUD helpers for supplementary task messages."""
from __future__ import annotations

import uuid
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.models import TaskMessage


async def add_task_message(
    task_id: str,
    content: str,
    *,
    role: str = "user",
    source: str = "api",
    db: Optional[AsyncSession] = None,
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
