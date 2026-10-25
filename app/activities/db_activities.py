import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from temporalio import activity

from app.db.database import AsyncSessionLocal
from app.metrics import inc as metrics_inc
from app.production.alerting import send_alert
from app.status import RUN_STATE_FOR_TASK_STATUS, RUN_TERMINAL, TERMINAL, RunState, StepStatus, TaskStatus
from app.telemetry import traced_activity
from app.transitions import KEEP, set_run_state, set_task_status

logger = logging.getLogger("rmp.db_activities")
from app.db.models import (
    Event,
    MemoryItem,
    Observation,
    ProcessRun,
    Step,
    Task,
    TaskMessage,
)


def _hash_payload(data: Any) -> str:
    raw = str(data).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _actor(payload: Dict[str, Any]) -> str:
    """Who writes a status through these activities: the workflow that scheduled the activity, or, for a direct call
    (the reconciler, spawn), the ``actor`` its payload names."""
    if activity.in_activity():
        return f"workflow:{activity.info().workflow_type}"
    return payload["actor"]


def _next_check(minutes: Optional[int]) -> Any:
    """The next check ``minutes`` from now (naive UTC), or KEEP the stored one."""
    if minutes is None:
        return KEEP
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes)


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
        if task.status in TERMINAL:
            return False
        task.updated_at = datetime.utcnow()
        await db.commit()
    return True


@traced_activity("task.update_status")
async def update_task_status(payload: Dict[str, Any]) -> bool:
    """Move the task to ``status``; True only when this call changed it, and only then index, count and alert."""
    task_id = payload.get("task_id")
    status = payload.get("status")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            return False
        transition = await set_task_status(
            db, task_id, status, actor=_actor(payload), cause=payload.get("cause") or "update_task_status",
            next_check_at=_next_check(payload.get("next_check_minutes")))
        await db.commit()
        if not transition.changed:
            return False

        if status in TERMINAL:
            from app.task_registry.hooks import index_terminal_task_async

            await index_terminal_task_async(task_id)

        if status == TaskStatus.COMPLETED:
            metrics_inc("task_completed")
        elif status == TaskStatus.FAILED:
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


@traced_activity("approval.confirm_provenance")
async def confirm_approval_provenance(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Whether an approval came from Kirill's Slack user, in a message recorded after the gate opened.

    Only messages that arrived through Slack carry a sender; an API signal or a message without one is
    refused, and the refusal is recorded. ``gate_opened_at`` is UTC (ISO 8601).
    """
    from app.config import get_slack_owner_user_id
    from app.workflows.approval import gate_decision

    task_id = payload["task_id"]
    opened = datetime.fromisoformat(payload["gate_opened_at"]).replace(tzinfo=None)
    owner = get_slack_owner_user_id()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(TaskMessage)
            .where(TaskMessage.task_id == task_id, TaskMessage.role == "user", TaskMessage.source == "slack",
                   TaskMessage.created_at >= opened)
            .order_by(TaskMessage.created_at.desc())
        )).scalars().all()
        for row in rows:
            slack = (row.meta or {}).get("slack") or {}
            if owner and slack.get("user_id") == owner and gate_decision(row.content) == "approve":
                confirmed = {"ok": True, "message_id": row.id, "slack_ts": row.slack_ts, "slack_user_id": owner,
                             "event_ts": slack.get("event_ts")}
                db.add(Event(correlation_id=task_id, entity_type="task", entity_id=task_id, event_type="approval.confirmed",
                             event_payload={"gate_opened_at": payload["gate_opened_at"], **confirmed}))
                await db.commit()
                return confirmed
        senders = sorted({str(((r.meta or {}).get("slack") or {}).get("user_id")) for r in rows})
        reason = (f"no Slack approval from {owner or 'the owner (unset)'} after {opened.isoformat()}"
                  f" ({len(rows)} Slack message(s) since, senders {senders})")
        db.add(Event(correlation_id=task_id, entity_type="task", entity_id=task_id, event_type="approval.refused",
                     event_payload={"gate_opened_at": payload["gate_opened_at"], "reason": reason}))
        await db.commit()
    logger.warning("Approval refused for task %s: %s", task_id[:8], reason)
    return {"ok": False, "reason": reason}


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
            and existing.current_state not in RUN_TERMINAL
            and not force_new
        ):
            return existing.id
        parent_run_id = None
        if force_new and existing:
            parent_run_id = existing.id
            if (
                durable_leg
                and existing.current_state not in RUN_TERMINAL
            ):
                await set_run_state(
                    db, existing.id, RunState.SUPERSEDED, actor=_actor(payload),
                    cause=payload.get("cause") or "ensure_process_run", ended=True, release_lease=True)

        run_id = str(uuid.uuid4())
        db.add(
            ProcessRun(
                id=run_id,
                task_id=task_id,
                process_type=process_type,
                current_state=RunState.RUNNING,
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
    """Move the run to ``state`` with its lease, end and next check; True only when this call changed its state.
    Without a state, only those fields change."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ProcessRun).where(ProcessRun.id == payload.get("process_run_id"))
        )
        run = result.scalar_one_or_none()
        if not run:
            return False
        if "state" in payload:
            # A task spelling handed in is stored as the run's (rule 16): the catalog's _stop_task sends the task's
            # "cancelled", and a run is "canceled". Without a state, the stored spelling stays as it is.
            state = payload["state"]
            ended = bool(payload.get("ended"))
            transition = await set_run_state(
                db, run.id, state if state in RunState else RUN_STATE_FOR_TASK_STATUS.get(state, state),
                actor=_actor(payload), cause=payload.get("cause") or "update_process_state", ended=ended,
                release_lease=ended or bool(payload.get("release_lease")),
                next_check_at=_next_check(payload.get("next_check_minutes")))
            await db.commit()
            return transition.changed
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
    """Align task + process run on terminal failure and release lease.

    True when this call ended the task (without a task, the run); the task is indexed only then.
    """
    task_id = payload.get("task_id")
    process_run_id = payload.get("process_run_id")
    actor, cause = _actor(payload), payload.get("cause") or "finalize_task_failure"
    task_moved = run_moved = None
    async with AsyncSessionLocal() as db:
        if task_id:
            task_moved = await set_task_status(
                db, task_id, payload.get("task_status", TaskStatus.FAILED), actor=actor, cause=cause,
                closed_reason=payload.get("closed_reason"))
        if process_run_id:
            run_moved = await set_run_state(
                db, process_run_id, payload.get("process_state", RunState.FAILED_TERMINAL), actor=actor, cause=cause,
                ended=True, release_lease=True)
        await db.commit()
    if task_moved is not None and task_moved.changed:
        from app.task_registry.hooks import index_terminal_task_async

        await index_terminal_task_async(task_id)
    moved = task_moved if task_moved is not None else run_moved
    return moved is not None and moved.changed


@traced_activity("process.record_compensation")
async def record_compensation(payload: Dict[str, Any]) -> bool:
    """Mark task/process as compensated after rollback or partial failure cleanup; True when the task changed."""
    task_id = payload.get("task_id")
    process_run_id = payload.get("process_run_id")
    reason = payload.get("reason", "Compensation applied")
    actor, cause = _actor(payload), payload.get("cause") or "record_compensation"
    task_changed = False
    async with AsyncSessionLocal() as db:
        if task_id:
            moved = await set_task_status(db, task_id, TaskStatus.COMPENSATED, actor=actor, cause=cause)
            task_changed = moved.changed
        if process_run_id:
            await set_run_state(db, process_run_id, RunState.COMPENSATED, actor=actor, cause=cause, ended=True,
                                release_lease=True)
        if task_changed:
            db.add(
                Event(
                    correlation_id=task_id,
                    entity_type="task",
                    entity_id=task_id,
                    event_type="task.compensated",
                    event_payload={"reason": reason[:500], "process_run_id": process_run_id},
                )
            )
        await db.commit()
    if task_changed:
        from app.task_registry.hooks import index_terminal_task_async

        await index_terminal_task_async(task_id)
    return task_changed


@traced_activity("process.execute_compensation")
async def execute_compensation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Release lease, mark open steps compensated, annotate memory, set terminal state.

    The steps and the memory note follow the run, the ``task.compensated`` event the task: each only when this call
    moved it. ``compensated`` in the result says whether it moved the task.
    """
    from app.memory.router import MemoryRouter

    task_id = payload.get("task_id")
    process_run_id = payload.get("process_run_id")
    reason = (payload.get("reason") or "Compensation applied")[:500]
    owner = payload.get("owner", task_id)
    actor, cause = _actor(payload), payload.get("cause") or "execute_compensation"

    steps_marked = 0
    run_changed = task_changed = False
    async with AsyncSessionLocal() as db:
        if process_run_id:
            pr = await db.execute(
                select(ProcessRun).where(ProcessRun.id == process_run_id)
            )
            run = pr.scalar_one_or_none()
            if run:
                # The lease is released only for its owner, or when it has none.
                released = not owner or not run.lease_owner or run.lease_owner == owner
                moved = await set_run_state(db, run.id, RunState.COMPENSATED, actor=actor, cause=cause, ended=True,
                                            release_lease=released)
                run_changed = moved.changed
            if run_changed:
                step_result = await db.execute(
                    select(Step).where(
                        Step.process_run_id == process_run_id,
                        Step.status.in_((StepStatus.RUNNING, StepStatus.PENDING)),
                    )
                )
                for step in step_result.scalars().all():
                    step.status = StepStatus.COMPENSATED
                    step.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    steps_marked += 1
        if task_id:
            moved = await set_task_status(db, task_id, TaskStatus.COMPENSATED, actor=actor, cause=cause)
            task_changed = moved.changed
        if task_changed:
            db.add(
                Event(
                    correlation_id=task_id,
                    entity_type="task",
                    entity_id=task_id,
                    event_type="task.compensated",
                    event_payload={
                        "reason": reason,
                        "process_run_id": process_run_id,
                        "steps_marked": steps_marked,
                    },
                )
            )
        await db.commit()

    if run_changed:
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
        "compensated": task_changed,
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
                    if existing.status == StepStatus.RUNNING:
                        existing.status = payload.get("status", StepStatus.COMPLETED)
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
    """Aura's memory block: the Internal Agent's fast context (app/deep_memory/curator.py)."""
    import asyncio

    from app.deep_memory.curator import assemble_fast_context

    try:
        skip_vector = bool(payload.get("skip_vector"))
        return await assemble_fast_context(
            task_id=payload.get("task_id"),
            process_run_id=payload.get("process_run_id", ""),
            process_type=payload.get("process_type", "generic"),
            query=None if skip_vector else (payload.get("semantic_query") or payload.get("query")),
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
    )
