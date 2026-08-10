"""Build denormalized task registry summaries from ledger rows."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.models import Artifact, Event, ProcessRun, Task, TaskRegistryEntry


def _snippet(text: str, limit: int = 400) -> str:
    t = (text or "").strip()
    return t if len(t) <= limit else t[: limit - 3] + "..."


async def build_task_summary(
    task_id: str,
    db: Optional[AsyncSession] = None,
) -> Dict[str, Any]:
    async def _build(session: AsyncSession) -> Dict[str, Any]:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            return {}

        pr_result = await session.execute(
            select(ProcessRun)
            .where(ProcessRun.task_id == task_id)
            .order_by(ProcessRun.started_at.asc())
        )
        runs = pr_result.scalars().all()
        process_type = runs[0].process_type if runs else task.task_type or "generic"

        ended_at = None
        duration_sec = None
        if task.created_at:
            for run in reversed(runs):
                if run.ended_at:
                    ended_at = run.ended_at
                    break
            end = ended_at or task.updated_at or datetime.utcnow()
            duration_sec = max(0, int((end - task.created_at).total_seconds()))

        ev_result = await session.execute(
            select(Event)
            .where(Event.entity_id == task_id)
            .order_by(Event.occurred_at.desc())
            .limit(5)
        )
        events = ev_result.scalars().all()
        outcome_parts: List[str] = [f"status={task.status}"]
        for ev in events:
            if ev.event_type in (
                "task.completed",
                "task.failed",
                "reconciler.stuck_repaired",
                "intake.decided",
            ):
                payload = ev.event_payload or {}
                if payload.get("reason"):
                    outcome_parts.append(str(payload["reason"])[:120])
                elif payload.get("decision"):
                    outcome_parts.append(f"decision={payload['decision']}")

        artifact_refs: List[str] = []
        if runs:
            art_result = await session.execute(
                select(Artifact)
                .where(Artifact.process_run_id.in_([r.id for r in runs]))
                .limit(10)
            )
            for art in art_result.scalars().all():
                artifact_refs.append(f"{art.kind}:{art.filename or art.id}")

        return {
            "task_id": task.id,
            "intent_snippet": _snippet(task.goal or ""),
            "outcome_summary": "; ".join(outcome_parts)[:800],
            "process_type": process_type,
            "terminal_status": task.status,
            "task_kind": task.task_kind or "one_shot",
            "recurrence_key": task.recurrence_key,
            "session_key": task.openclaw_session_key,
            "duration_sec": duration_sec,
            "artifact_refs": artifact_refs,
            "task_created_at": task.created_at,
            "task_ended_at": ended_at or task.updated_at,
        }

    if db is not None:
        return await _build(db)
    async with AsyncSessionLocal() as session:
        return await _build(session)


async def upsert_registry_entry(
    task_id: str,
    db: Optional[AsyncSession] = None,
    vector_point_id: Optional[str] = None,
) -> Optional[str]:
    summary = await build_task_summary(task_id, db=db)
    if not summary:
        return None

    async def _upsert(session: AsyncSession) -> str:
        result = await session.execute(
            select(TaskRegistryEntry).where(TaskRegistryEntry.task_id == task_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            import uuid

            row = TaskRegistryEntry(id=str(uuid.uuid4()), task_id=task_id)
            session.add(row)
        row.intent_snippet = summary["intent_snippet"]
        row.outcome_summary = summary["outcome_summary"]
        row.process_type = summary["process_type"]
        row.terminal_status = summary["terminal_status"]
        row.task_kind = summary["task_kind"]
        row.recurrence_key = summary["recurrence_key"]
        row.session_key = summary["session_key"]
        row.duration_sec = summary["duration_sec"]
        row.artifact_refs = summary["artifact_refs"]
        row.task_created_at = summary["task_created_at"]
        row.task_ended_at = summary["task_ended_at"]
        row.indexed_at = datetime.utcnow()
        if vector_point_id:
            row.vector_point_id = vector_point_id
        await session.commit()
        return row.id

    if db is not None:
        result = await db.execute(
            select(TaskRegistryEntry).where(TaskRegistryEntry.task_id == task_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            import uuid

            row = TaskRegistryEntry(id=str(uuid.uuid4()), task_id=task_id)
            db.add(row)
        row.intent_snippet = summary["intent_snippet"]
        row.outcome_summary = summary["outcome_summary"]
        row.process_type = summary["process_type"]
        row.terminal_status = summary["terminal_status"]
        row.task_kind = summary["task_kind"]
        row.recurrence_key = summary["recurrence_key"]
        row.session_key = summary["session_key"]
        row.duration_sec = summary["duration_sec"]
        row.artifact_refs = summary["artifact_refs"]
        row.task_created_at = summary["task_created_at"]
        row.task_ended_at = summary["task_ended_at"]
        row.indexed_at = datetime.utcnow()
        if vector_point_id:
            row.vector_point_id = vector_point_id
        await db.flush()
        return row.id
    async with AsyncSessionLocal() as session:
        return await _upsert(session)
