"""Hybrid retrieval: Postgres filters + vector similarity + temporal decay."""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_task_registry_config
from app.db.database import AsyncSessionLocal
from app.db.models import Task, TaskRegistryEntry
from app.task_registry.vector_store import search_similar_tasks

logger = logging.getLogger("rmp.task_registry.retriever")

ACTIVE_STATUSES = frozenset(
    {"created", "running", "pending", "pending_user_input", "blocked", "needs_replan"}
)


def _apply_temporal_decay(
    hits: List[Dict[str, Any]],
    registry_rows: List[Dict[str, Any]],
    *,
    half_life_days: float,
) -> List[Dict[str, Any]]:
    ended_by_task = {
        r.get("task_id"): r.get("task_ended_at") for r in registry_rows if r.get("task_id")
    }
    now = datetime.utcnow()
    decayed: List[Dict[str, Any]] = []
    for hit in hits:
        tid = hit.get("task_id")
        score = float(hit.get("score") or 0)
        ended_raw = ended_by_task.get(tid)
        age_days = 0.0
        if ended_raw:
            try:
                if isinstance(ended_raw, str):
                    ended = datetime.fromisoformat(ended_raw.replace("Z", "+00:00"))
                    if ended.tzinfo:
                        ended = ended.replace(tzinfo=None)
                else:
                    ended = ended_raw
                age_days = max(0.0, (now - ended).total_seconds() / 86400.0)
            except Exception:
                age_days = 0.0
        factor = math.exp(-age_days / max(half_life_days, 1.0))
        hit = dict(hit)
        hit["score"] = score * factor
        hit["decayed_score"] = hit["score"]
        hit["age_days"] = age_days
        decayed.append(hit)
    decayed.sort(key=lambda h: h.get("score") or 0, reverse=True)
    return decayed


async def fetch_active_tasks(
    *,
    session_key: Optional[str] = None,
    recurrence_key: Optional[str] = None,
    limit: int = 10,
    db: Optional[AsyncSession] = None,
) -> List[Dict[str, Any]]:
    async def _query(session: AsyncSession) -> List[Dict[str, Any]]:
        q = select(Task).where(Task.status.in_(list(ACTIVE_STATUSES)))
        if session_key:
            q = q.where(Task.openclaw_session_key == session_key)
        if recurrence_key:
            q = q.where(Task.recurrence_key == recurrence_key)
        q = q.order_by(Task.updated_at.desc()).limit(limit)
        result = await session.execute(q)
        rows = []
        for t in result.scalars().all():
            rows.append(
                {
                    "task_id": t.id,
                    "status": t.status,
                    "task_type": t.task_type,
                    "task_kind": t.task_kind,
                    "recurrence_key": t.recurrence_key,
                    "session_key": t.openclaw_session_key,
                    "goal": t.goal or "",
                    "goal_snippet": (t.goal or "")[:300],
                    "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                }
            )
        return rows

    if db is not None:
        return await _query(db)
    async with AsyncSessionLocal() as session:
        return await _query(session)


async def fetch_recent_registry(
    *,
    recurrence_key: Optional[str] = None,
    session_key: Optional[str] = None,
    days: int = 90,
    limit: int = 10,
    db: Optional[AsyncSession] = None,
) -> List[Dict[str, Any]]:
    cutoff = datetime.utcnow() - timedelta(days=days)

    async def _query(session: AsyncSession) -> List[Dict[str, Any]]:
        q = select(TaskRegistryEntry).where(
            TaskRegistryEntry.indexed_at >= cutoff
        )
        if recurrence_key:
            q = q.where(TaskRegistryEntry.recurrence_key == recurrence_key)
        if session_key:
            q = q.where(TaskRegistryEntry.session_key == session_key)
        q = q.order_by(TaskRegistryEntry.task_ended_at.desc().nullslast()).limit(limit)
        result = await session.execute(q)
        out = []
        for row in result.scalars().all():
            out.append(
                {
                    "task_id": row.task_id,
                    "terminal_status": row.terminal_status,
                    "process_type": row.process_type,
                    "intent_snippet": row.intent_snippet,
                    "outcome_summary": row.outcome_summary,
                    "recurrence_key": row.recurrence_key,
                    "session_key": row.session_key,
                    "task_ended_at": row.task_ended_at.isoformat()
                    if row.task_ended_at
                    else None,
                }
            )
        return out

    if db is not None:
        return await _query(db)
    async with AsyncSessionLocal() as session:
        return await _query(session)


async def hybrid_search(
    intent: str,
    *,
    session_key: Optional[str] = None,
    recurrence_key: Optional[str] = None,
    limit: int = 5,
) -> Dict[str, Any]:
    cfg = get_task_registry_config()
    deadline = float(cfg.get("intake_vector_deadline_sec", 10))
    return await hybrid_search_bounded(
        intent,
        session_key=session_key,
        recurrence_key=recurrence_key,
        limit=limit,
        deadline_sec=deadline,
    )


async def _vector_similar_bounded(
    intent: str,
    *,
    limit: int,
    min_score: float,
    deadline_sec: float,
) -> List[Dict[str, Any]]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                search_similar_tasks,
                intent,
                limit=limit,
                min_score=min_score,
            ),
            timeout=deadline_sec,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Intake vector search timed out after %.1fs; continuing without vector_similar",
            deadline_sec,
        )
        return []
    except Exception as exc:
        logger.warning("Intake vector search failed: %s", exc)
        return []


async def hybrid_search_bounded(
    intent: str,
    *,
    session_key: Optional[str] = None,
    recurrence_key: Optional[str] = None,
    limit: int = 5,
    deadline_sec: float = 10.0,
) -> Dict[str, Any]:
    cfg = get_task_registry_config()
    min_score = float(cfg.get("similarity_threshold", 0.72))
    half_life = float(cfg.get("temporal_half_life_days", 30))
    # Independent I/O: overlap Postgres legs with bounded vector search.
    active, recent, vector_hits = await asyncio.gather(
        fetch_active_tasks(
            session_key=session_key,
            recurrence_key=recurrence_key,
            limit=limit,
        ),
        fetch_recent_registry(
            recurrence_key=recurrence_key,
            session_key=session_key,
            limit=limit,
        ),
        _vector_similar_bounded(
            intent,
            limit=limit,
            min_score=min_score * 0.5,
            deadline_sec=deadline_sec,
        ),
    )
    vector_hits = _apply_temporal_decay(vector_hits, recent, half_life_days=half_life)
    return {
        "active_tasks": active,
        "recent_registry": recent,
        "vector_similar": vector_hits,
    }
