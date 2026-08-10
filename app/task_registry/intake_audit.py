"""Persist and query task intake decision audit rows."""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.models import TaskIntakeDecision


async def record_intake_decision(
    *,
    request_hash: str,
    decision: str,
    confidence: int,
    rationale: str,
    similar_task_ids: Optional[List[str]] = None,
    llm_raw: Optional[Dict[str, Any]] = None,
    policy_overrides: Optional[List[str]] = None,
    intake_mode: str = "shadow",
    session_key: str = "",
    intent_snippet: str = "",
    db: Optional[AsyncSession] = None,
) -> str:
    row_id = str(uuid.uuid4())
    row = TaskIntakeDecision(
        id=row_id,
        request_hash=request_hash,
        decision=decision,
        confidence=confidence,
        rationale=(rationale or "")[:4000],
        similar_task_ids=similar_task_ids or [],
        llm_raw=llm_raw,
        policy_overrides=policy_overrides or [],
        intake_mode=intake_mode,
        session_key=session_key,
        intent_snippet=(intent_snippet or "")[:500],
    )

    async def _commit(session: AsyncSession) -> None:
        session.add(row)
        await session.commit()

    if db is not None:
        db.add(row)
        await db.flush()
        return row_id
    async with AsyncSessionLocal() as session:
        await _commit(session)
    return row_id
