import hashlib
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update

from app.db.database import AsyncSessionLocal
from app.metrics import inc as metrics_inc
from app.production.alerting import send_alert
from app.telemetry import traced_activity

logger = logging.getLogger("rmp.db_activities")
from app.db.models import (
    Event,
    MemoryItem,
    Observation,
    ProcessRun,
    Step,
    Task,
)


_TERMINAL_PROCESS_STATES = frozenset(
    {"completed", "failed_terminal", "stopped_by_user", "compensated", "canceled"}
)


def _hash_payload(data: Any) -> str:
    raw = str(data).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


@traced_activity("task.touch_liveness")
async def touch_task_liveness(payload: Dict[str, Any]) -> bool:
    """Refresh tasks.updated_at while long OpenClaw polls run (reconciler liveness)."""
    task_id = payload.get("task_id")
    if not task_id:
        return False
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            return False
        if task.status in ("completed", "failed", "compensated", "stopped_by_user", "cancelled"):
            return False
        task.updated_at = datetime.utcnow()
        await db.commit()
    return True


@traced_activity("task.update_status")
async def update_task_status(payload: Dict[str, Any]) -> bool:
    task_id = payload.get("task_id")
    status = payload.get("status")
    next_check_minutes = payload.get("next_check_minutes")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            return False
        task.status = status
        if next_check_minutes is not None:
            task.next_check_at = datetime.utcnow() + timedelta(minutes=next_check_minutes)
        elif status in ("completed", "failed", "stopped_by_user", "compensated"):
            task.next_check_at = None
        await db.commit()

        if status in ("completed", "failed", "stopped_by_user", "compensated", "cancelled"):
            from app.task_registry.hooks import index_terminal_task_async

            await index_terminal_task_async(task_id)

        if status == "completed":
            metrics_inc("task_completed")
        elif status == "failed":
            metrics_inc("task_failed")
            from app.notification_policy import is_internal_task

            if not is_internal_task(task.goal or "", task.task_type or ""):
                await send_alert(
                    "task.failed",
                    f"Task {task_id[:8]} failed",
                    severity="error",
                    context={
                        "task_id": task_id,
                        "goal": (task.goal or "")[:200],
                        "task_type": task.task_type,
                    },
                )
        return True


@traced_activity("task.record_event")
async def record_event(payload: Dict[str, Any]) -> str:
    event_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        db.add(
            Event(
                id=event_id,
                correlation_id=payload.get("correlation_id"),
                entity_type=payload.get("entity_type"),
                entity_id=payload.get("entity_id"),
                event_type=payload.get("event_type"),
                event_payload=payload.get("event_payload"),
            )
        )
        await db.commit()
    return event_id


@traced_activity("process.ensure_run")
async def ensure_process_run(payload: Dict[str, Any]) -> str:
    task_id = payload.get("task_id")
    process_type = payload.get("process_type", "generic_task")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ProcessRun)
            .where(ProcessRun.task_id == task_id)
            .order_by(ProcessRun.started_at.desc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()
        force_new = bool(payload.get("force_new"))
        durable_leg = bool(payload.get("durable_leg"))
        if (
            existing
            and existing.current_state not in _TERMINAL_PROCESS_STATES
            and not force_new
        ):
            return existing.id
        parent_run_id = None
        if force_new and existing:
            parent_run_id = existing.id
            if (
                durable_leg
                and existing.current_state not in _TERMINAL_PROCESS_STATES
            ):
                existing.current_state = "superseded"
                existing.ended_at = datetime.utcnow()
                existing.lease_owner = None

        run_id = str(uuid.uuid4())
        db.add(
            ProcessRun(
                id=run_id,
                task_id=task_id,
                process_type=process_type,
                current_state="running",
                success_criteria=payload.get("success_criteria"),
                parent_process_run_id=parent_run_id,
                next_check_at=datetime.utcnow() + timedelta(minutes=5),
            )
        )
        await db.commit()
        return run_id


@traced_activity("process.acquire_lease")
async def acquire_process_run_lease(payload: Dict[str, Any]) -> Dict[str, Any]:
    process_run_id = payload.get("process_run_id")
    owner = payload.get("owner", "")
    if not process_run_id or not owner:
        return {"acquired": False, "reason": "missing_params"}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ProcessRun).where(ProcessRun.id == process_run_id)
        )
        run = result.scalar_one_or_none()
        if not run:
            return {"acquired": False, "reason": "not_found"}
        if run.lease_owner and run.lease_owner != owner:
            return {
                "acquired": False,
                "reason": "held_by_other",
                "lease_owner": run.lease_owner,
            }
        run.lease_owner = owner
        await db.commit()
        return {"acquired": True, "lease_owner": owner}


@traced_activity("process.update_state")
async def update_process_state(payload: Dict[str, Any]) -> bool:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ProcessRun).where(ProcessRun.id == payload.get("process_run_id"))
        )
        run = result.scalar_one_or_none()
        if not run:
            return False
        run.current_state = payload.get("state", run.current_state)
        if payload.get("ended"):
            run.ended_at = datetime.utcnow()
            run.lease_owner = None
        if payload.get("release_lease"):
            run.lease_owner = None
        if payload.get("next_check_minutes") is not None:
            run.next_check_at = datetime.utcnow() + timedelta(
                minutes=payload["next_check_minutes"]
            )
        await db.commit()
        return True


@traced_activity("process.release_lease")
async def release_process_run_lease(payload: Dict[str, Any]) -> Dict[str, Any]:
    process_run_id = payload.get("process_run_id")
    owner = payload.get("owner")
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ProcessRun).where(ProcessRun.id == process_run_id)
        )
        run = result.scalar_one_or_none()
        if not run:
            return {"released": False, "reason": "not_found"}
        if owner and run.lease_owner and run.lease_owner != owner:
            return {"released": False, "reason": "owner_mismatch"}
        run.lease_owner = None
        await db.commit()
        return {"released": True}


@traced_activity("process.finalize_failure")
async def finalize_task_failure(payload: Dict[str, Any]) -> bool:
    """Align task + process run on terminal failure and release lease."""
    task_id = payload.get("task_id")
    process_run_id = payload.get("process_run_id")
    state = payload.get("process_state", "failed_terminal")
    async with AsyncSessionLocal() as db:
        if task_id:
            tr = await db.execute(select(Task).where(Task.id == task_id))
            task = tr.scalar_one_or_none()
            if task:
                task.status = payload.get("task_status", "failed")
                task.next_check_at = None
        if process_run_id:
            pr = await db.execute(
                select(ProcessRun).where(ProcessRun.id == process_run_id)
            )
            run = pr.scalar_one_or_none()
            if run:
                run.current_state = state
                run.ended_at = datetime.utcnow()
                run.lease_owner = None
        await db.commit()
    from app.task_registry.hooks import index_terminal_task_async

    await index_terminal_task_async(task_id)
    return True


@traced_activity("process.record_compensation")
async def record_compensation(payload: Dict[str, Any]) -> bool:
    """Mark task/process as compensated after rollback or partial failure cleanup."""
    task_id = payload.get("task_id")
    process_run_id = payload.get("process_run_id")
    reason = payload.get("reason", "Compensation applied")
    async with AsyncSessionLocal() as db:
        if task_id:
            tr = await db.execute(select(Task).where(Task.id == task_id))
            task = tr.scalar_one_or_none()
            if task:
                task.status = "compensated"
                task.next_check_at = None
        if process_run_id:
            pr = await db.execute(
                select(ProcessRun).where(ProcessRun.id == process_run_id)
            )
            run = pr.scalar_one_or_none()
            if run:
                run.current_state = "compensated"
                run.ended_at = datetime.utcnow()
                run.lease_owner = None
        db.add(
            Event(
                correlation_id=task_id or process_run_id or str(uuid.uuid4()),
                entity_type="task",
                entity_id=task_id or "",
                event_type="task.compensated",
                event_payload={"reason": reason[:500], "process_run_id": process_run_id},
            )
        )
        await db.commit()
    if task_id:
        from app.task_registry.hooks import index_terminal_task_async

        await index_terminal_task_async(task_id)
    return True


@traced_activity("process.execute_compensation")
async def execute_compensation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Release lease, mark open steps compensated, annotate memory, set terminal state."""
    from app.memory.router import MemoryRouter

    task_id = payload.get("task_id")
    process_run_id = payload.get("process_run_id")
    reason = (payload.get("reason") or "Compensation applied")[:500]
    owner = payload.get("owner", task_id)

    steps_marked = 0
    async with AsyncSessionLocal() as db:
        if process_run_id:
            pr = await db.execute(
                select(ProcessRun).where(ProcessRun.id == process_run_id)
            )
            run = pr.scalar_one_or_none()
            if run:
                if not owner or not run.lease_owner or run.lease_owner == owner:
                    run.lease_owner = None
                run.current_state = "compensated"
                run.ended_at = datetime.utcnow()
            step_result = await db.execute(
                select(Step).where(
                    Step.process_run_id == process_run_id,
                    Step.status.in_(("running", "pending")),
                )
            )
            for step in step_result.scalars().all():
                step.status = "compensated"
                step.ended_at = datetime.utcnow()
                steps_marked += 1
        if task_id:
            tr = await db.execute(select(Task).where(Task.id == task_id))
            task = tr.scalar_one_or_none()
            if task:
                task.status = "compensated"
                task.next_check_at = None
        db.add(
            Event(
                correlation_id=task_id or process_run_id or str(uuid.uuid4()),
                entity_type="task",
                entity_id=task_id or "",
                event_type="task.compensated",
                event_payload={
                    "reason": reason,
                    "process_run_id": process_run_id,
                    "steps_marked": steps_marked,
                },
            )
        )
        await db.commit()

    if process_run_id:
        try:
            await MemoryRouter.write(
                scope_type="process",
                scope_id=process_run_id,
                memory_type="working",
                content=f"Compensated: {reason}",
                provenance={"task_id": task_id, "compensation": True},
                confidence=100,
            )
        except Exception as exc:
            logger.warning("Compensation memory annotation skipped: %s", exc)

    return {
        "compensated": True,
        "steps_marked": steps_marked,
        "task_id": task_id,
        "process_run_id": process_run_id,
    }


@traced_activity("process.record_step")
async def record_step(payload: Dict[str, Any]) -> str:
    process_run_id = payload.get("process_run_id")
    idem_key = payload.get("idempotency_key")
    async with AsyncSessionLocal() as db:
        if idem_key and process_run_id:
            existing_result = await db.execute(
                select(Step).where(
                    Step.process_run_id == process_run_id,
                    Step.idempotency_key == idem_key,
                )
            )
            existing = existing_result.scalar_one_or_none()
            if existing:
                if payload.get("status"):
                    existing.status = payload.get("status")
                if payload.get("output_ref") is not None:
                    existing.output_ref = payload.get("output_ref")
                if payload.get("ended"):
                    existing.ended_at = datetime.utcnow()
                    if existing.status == "running":
                        existing.status = payload.get("status", "completed")
                await db.commit()
                return existing.id

        step_id = str(uuid.uuid4())
        db.add(
            Step(
                id=step_id,
                process_run_id=process_run_id,
                step_name=payload.get("step_name"),
                step_kind=payload.get("step_kind", "execution"),
                status=payload.get("status", "running"),
                attempt_no=payload.get("attempt_no", 1),
                idempotency_key=idem_key,
                input_ref=payload.get("input_ref"),
                output_ref=payload.get("output_ref"),
                started_at=datetime.utcnow(),
                ended_at=datetime.utcnow() if payload.get("ended") else None,
            )
        )
        await db.commit()
    return step_id


@traced_activity("process.record_observation")
async def record_observation(payload: Dict[str, Any]) -> str:
    obs_id = str(uuid.uuid4())
    content = payload.get("payload", {})
    async with AsyncSessionLocal() as db:
        db.add(
            Observation(
                id=obs_id,
                process_run_id=payload.get("process_run_id"),
                source=payload.get("source", "openclaw"),
                observation_type=payload.get("observation_type", "agent_output"),
                payload_ref=content,
                payload_hash=_hash_payload(content),
                confidence=payload.get("confidence", 100),
            )
        )
        await db.commit()
    return obs_id


@traced_activity("memory.write")
async def write_process_memory(payload: Dict[str, Any]) -> str:
    """Write process-scoped working memory (Postgres + vector index when applicable)."""
    from app.memory.router import MemoryRouter

    return await MemoryRouter.write(
        scope_type=payload.get("scope_type", "process"),
        scope_id=payload.get("scope_id"),
        memory_type=payload.get("memory_type", "working"),
        content=payload.get("content", ""),
        provenance=payload.get("provenance") or payload.get("provenance_ref"),
        confidence=payload.get("confidence", 100),
    )


@traced_activity("memory.build_context")
async def build_process_memory_context(payload: Dict[str, Any]) -> str:
    import asyncio

    from app.memory.router import MemoryRouter

    try:
        skip_vector = bool(payload.get("skip_vector"))
        return await MemoryRouter.build_context_block(
            process_run_id=payload.get("process_run_id", ""),
            query=None if skip_vector else (payload.get("semantic_query") or payload.get("query")),
            task_id=payload.get("task_id"),
            process_type=payload.get("process_type", "generic"),
            user_scope_id=payload.get("user_scope_id", "default"),
            skip_vector=skip_vector,
        )
    except (asyncio.CancelledError, Exception) as exc:
        logger.warning("build_process_memory_context fail-soft: %s", exc)
        return ""


@traced_activity("memory.write_episodic")
async def write_episodic_observation(payload: Dict[str, Any]) -> str:
    """Auto-write episodic memory from agent observation text."""
    import asyncio

    from app.memory.router import MemoryRouter

    text = (payload.get("text") or "").strip()
    if len(text) < 20:
        return ""
    content = text[:2000]
    try:
        return await MemoryRouter.write(
            scope_type="process",
            scope_id=payload.get("process_run_id"),
            memory_type="episodic",
            content=content,
            provenance={
                "task_id": payload.get("task_id"),
                "step_id": payload.get("step_id"),
                "source": payload.get("source", "openclaw"),
            },
            confidence=payload.get("confidence", 90),
        )
    except (asyncio.CancelledError, Exception) as exc:
        logger.warning("write_episodic_observation fail-soft: %s", exc)
        return ""


@traced_activity("memory.read")
async def read_process_memory(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    from app.memory.router import MemoryRouter

    try:
        if payload.get("ordered"):
            return await MemoryRouter.read_ordered(
                process_run_id=payload.get("scope_id", ""),
                task_id=payload.get("task_id"),
                user_scope_id=payload.get("user_scope_id", "default"),
                process_type=payload.get("process_type", "generic"),
                query=payload.get("semantic_query") or payload.get("query"),
                limit=payload.get("limit", 20),
            )

        return await MemoryRouter.read(
            scope_type=payload.get("scope_type", "process"),
            scope_id=payload.get("scope_id"),
            memory_type=payload.get("memory_type"),
            limit=payload.get("limit", 20),
            query=payload.get("semantic_query") or payload.get("query"),
        )
    except Exception as exc:
        logger.warning("read_process_memory fail-soft: %s", exc)
        return []


@traced_activity("artifact.register")
async def register_artifact(payload: Dict[str, Any]) -> Dict[str, Any]:
    from app.artifacts.store import ArtifactStore, decode_content
    from app.config import is_artifact_store_enabled

    if not is_artifact_store_enabled():
        return {"skipped": True, "reason": "artifact_store disabled"}

    process_run_id = payload.get("process_run_id")
    kind = payload.get("kind", "blob")
    content = payload.get("content", "")
    encoding = payload.get("content_encoding", "utf-8")
    filename = payload.get("filename", f"{kind}.txt")
    mime_type = payload.get("mime_type")

    data = decode_content(content, encoding)
    return await ArtifactStore.store(
        process_run_id=process_run_id,
        kind=kind,
        data=data,
        filename=filename,
        mime_type=mime_type,
    )


@traced_activity("artifact.list")
async def list_process_artifacts(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    from app.artifacts.store import ArtifactStore

    return await ArtifactStore.list_for_process(payload.get("process_run_id", ""))


@traced_activity("memory.compact_episodic")
async def compact_episodic_memory(payload: Dict[str, Any]) -> Dict[str, Any]:
    from app.memory.router import MemoryRouter

    max_age_days = int(payload.get("max_age_days", 30))
    return await MemoryRouter.compact_episodic_memory(max_age_days)


@traced_activity("memory.promote_completion")
async def promote_completion_memory(payload: Dict[str, Any]) -> Dict[str, Any]:
    from app.memory.promotion import promote_completion_memory as promote

    return await promote(
        process_run_id=payload.get("process_run_id", ""),
        process_type=payload.get("process_type", ""),
        task_id=payload.get("task_id", ""),
        episodic_content=payload.get("content", ""),
        user_scope_id=payload.get("user_scope_id", "default"),
    )
