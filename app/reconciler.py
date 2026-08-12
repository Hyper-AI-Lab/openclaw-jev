"""Background reconciler: scans non-terminal tasks and stale process runs."""
import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from temporalio.client import Client, WorkflowExecutionStatus

from app.db.database import AsyncSessionLocal
from app.db.models import Event, ProcessRun, Task
from app.llm.quota_broker import reap_stale_llm_slots_sync
from app.metrics import inc as metrics_inc
from app.notification_policy import is_internal_task
from app.production.alerting import send_alert

logger = logging.getLogger("rmp.reconciler")

RECONCILE_INTERVAL_SEC = 60
STALE_TASK_MINUTES = 20
STUCK_REPAIR_MINUTES = 45
# Recover completed OpenClaw replies quickly after worker crashes mid-notify.
ORPHAN_REPLY_MIN_AGE_SEC = 90
ORPHAN_REPLY_MAX_AGE_MINUTES = 30

_temporal_client: Client | None = None


def _task_is_internal(task: Task) -> bool:
    return is_internal_task(task.goal or "", task.task_type or "", [])


async def _notify_repair(task: Task, message: str) -> None:
    if not task.openclaw_session_key:
        return
    if _task_is_internal(task):
        return
    try:
        from app.activities.openclaw_activities import notify_slack_user

        await notify_slack_user(
            {
                "session_key": task.openclaw_session_key,
                "task_id": task.id,
                "message": message,
            }
        )
    except Exception as exc:
        logger.warning("Reconciler Slack notify failed for %s: %s", task.id, exc)


async def _recover_orphaned_session_reply(
    client: Client,
    db,
    task: Task,
    now: datetime,
    stats: dict,
) -> bool:
    """If OpenClaw finished but workflow/Slack did not, deliver and complete."""
    if _task_is_internal(task):
        return False
    if task.status not in ("running", "created"):
        return False
    if not task.updated_at:
        return False
    age = now - task.updated_at
    if age < timedelta(seconds=ORPHAN_REPLY_MIN_AGE_SEC):
        return False
    if age > timedelta(minutes=ORPHAN_REPLY_MAX_AGE_MINUTES):
        return False

    # Skip if we already recovered once.
    prior = await db.execute(
        select(Event).where(
            Event.entity_id == task.id,
            Event.event_type == "reconciler.orphaned_reply_delivered",
        )
    )
    if prior.scalars().first():
        return False

    from app.orchestrator.session_recovery import (
        extract_user_facing_reply,
        read_completed_rmp_session_reply,
    )

    raw = read_completed_rmp_session_reply(task.id)
    if not raw:
        return False
    clean = extract_user_facing_reply(raw)
    if not clean or len(clean) < 40:
        return False

    await notify_slack_user_safe(task, clean)
    task.status = "completed"
    task.next_check_at = None

    wf_id = f"workflow-{task.id}"
    await _terminate_workflow(client, wf_id, "Orphan reply recovered — OpenClaw done, Slack delivered")
    orphans = await _cleanup_orphan_plan_children(client, task.id)
    stats["orphans_terminated"] = stats.get("orphans_terminated", 0) + orphans
    stats["orphaned_replies"] = stats.get("orphaned_replies", 0) + 1
    db.add(
        Event(
            correlation_id=task.correlation_id or task.id,
            entity_type="task",
            entity_id=task.id,
            event_type="reconciler.orphaned_reply_delivered",
            event_payload={"chars": len(clean), "terminated_children": orphans},
        )
    )
    stats["events"] += 1
    metrics_inc("reconciler_orphaned_reply_delivered")
    logger.info("Recovered orphaned reply for task %s (%d chars)", task.id[:8], len(clean))
    return True


async def notify_slack_user_safe(task: Task, message: str) -> None:
    if not task.openclaw_session_key:
        return
    try:
        from app.activities.openclaw_activities import notify_slack_user

        await notify_slack_user(
            {
                "session_key": task.openclaw_session_key,
                "task_id": task.id,
                "message": message,
                "intent": task.goal or "",
                "task_type": task.task_type or "",
                "tags": ["reconciler-recover"],
            }
        )
    except Exception as exc:
        logger.warning("Orphan reply Slack notify failed for %s: %s", task.id, exc)


async def _get_temporal() -> Client:
    global _temporal_client
    if _temporal_client is None:
        _temporal_client = await Client.connect("localhost:7233")
    return _temporal_client


async def _terminate_workflow(client: Client, wf_id: str, reason: str) -> bool:
    try:
        handle = client.get_workflow_handle(wf_id)
        await handle.terminate(reason)
        return True
    except Exception as exc:
        logger.warning("Terminate failed for %s: %s", wf_id, exc)
        return False


async def _cleanup_orphan_plan_children(client: Client, task_id: str) -> int:
    """Terminate orphaned plan child workflows when parent is terminal."""
    terminated = 0
    prefix = f"{task_id}-plan-"
    try:
        async for wf in client.list_workflows(f'WorkflowId STARTS_WITH "{prefix}"'):
            if wf.status != WorkflowExecutionStatus.RUNNING:
                continue
            if await _terminate_workflow(client, wf.id, "Orphan plan child — parent terminal"):
                terminated += 1
    except Exception as exc:
        logger.debug("Orphan child scan failed for %s: %s", task_id, exc)
    return terminated


async def _repair_stuck_running_task(
    client: Client,
    db,
    task: Task,
    now: datetime,
    stats: dict,
) -> bool:
    """Active repair: terminate RUNNING workflows stuck > STUCK_REPAIR_MINUTES."""
    if _task_is_internal(task):
        return False
    if task.status not in ("running", "created"):
        return False
    if task.status == "pending_user_input":
        return False
    repair_threshold = now - timedelta(minutes=STUCK_REPAIR_MINUTES)
    if task.updated_at and task.updated_at >= repair_threshold:
        return False

    wf_id = f"workflow-{task.id}"
    handle = client.get_workflow_handle(wf_id)
    try:
        desc = await handle.describe()
    except Exception as exc:
        logger.debug("Stuck repair describe failed for %s: %s", task.id, exc)
        return False

    if desc.status != WorkflowExecutionStatus.RUNNING:
        return False

    await _terminate_workflow(client, wf_id, f"Reconciler stuck repair (>={STUCK_REPAIR_MINUTES}m)")
    stats["terminated"] = stats.get("terminated", 0) + 1

    from app.activities.db_activities import execute_compensation, finalize_task_failure

    pr_result = await db.execute(
        select(ProcessRun).where(ProcessRun.task_id == task.id)
    )
    runs = pr_result.scalars().all()
    process_run_id = runs[0].id if runs else None
    has_progress = any(
        run.current_state not in ("created", "running", "waiting_agent")
        for run in runs
    )

    if has_progress and process_run_id:
        await execute_compensation(
            {
                "task_id": task.id,
                "process_run_id": process_run_id,
                "reason": f"Workflow stuck >{STUCK_REPAIR_MINUTES}m — reconciler terminated",
            }
        )
        task.status = "compensated"
    else:
        await finalize_task_failure(
            {
                "task_id": task.id,
                "process_run_id": process_run_id,
                "task_status": "failed",
                "process_state": "failed_terminal",
            }
        )
        task.status = "failed"

    task.next_check_at = None
    stats["repaired"] += 1
    orphans = await _cleanup_orphan_plan_children(client, task.id)
    stats["orphans_terminated"] = stats.get("orphans_terminated", 0) + orphans

    db.add(
        Event(
            correlation_id=task.correlation_id or task.id,
            entity_type="task",
            entity_id=task.id,
            event_type="reconciler.stuck_repaired",
            event_payload={
                "workflow_status": str(desc.status),
                "terminated_children": orphans,
            },
        )
    )
    stats["events"] += 1
    metrics_inc("reconciler_stuck_repaired")

    if not _task_is_internal(task):
        await _notify_repair(
            task,
            f"Task {task.id[:8]} was stuck and has been repaired by the reconciler.",
        )
    return True


async def count_stuck_running_workflows() -> int:
    """Count parent workflows still RUNNING while task row is non-active."""
    try:
        client = await _get_temporal()
        count = 0
        async for wf in client.list_workflows('ExecutionStatus="Running"'):
            if not wf.id.startswith("workflow-"):
                continue
            task_id = wf.id.removeprefix("workflow-")
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Task).where(Task.id == task_id))
                task = result.scalar_one_or_none()
                if task is None or task.status in (
                    "completed",
                    "failed",
                    "compensated",
                    "stopped_by_user",
                    "cancelled",
                ):
                    count += 1
                elif task.status == "running":
                    threshold = datetime.utcnow() - timedelta(minutes=STUCK_REPAIR_MINUTES)
                    if task.updated_at and task.updated_at < threshold:
                        count += 1
                elif task.status == "created":
                    threshold = datetime.utcnow() - timedelta(minutes=STUCK_REPAIR_MINUTES)
                    if task.updated_at and task.updated_at < threshold:
                        count += 1
        return count
    except Exception as exc:
        logger.warning("count_stuck_running_workflows failed: %s", exc)
        return -1


async def reconcile_once() -> dict:
    from app.config import is_development_mode

    if is_development_mode():
        return {"skipped": True, "reason": "development_mode"}
    stats = {
        "stale_tasks": 0,
        "re_signaled": 0,
        "repaired": 0,
        "terminated": 0,
        "orphans_terminated": 0,
        "orphaned_replies": 0,
        "events": 0,
        "llm_slots_reaped": 0,
    }
    reaped = reap_stale_llm_slots_sync()
    if reaped:
        stats["llm_slots_reaped"] = len(reaped)
        metrics_inc("llm_slots_reaped")
    now = datetime.utcnow()
    threshold = now - timedelta(minutes=STALE_TASK_MINUTES)
    repair_threshold = now - timedelta(minutes=STUCK_REPAIR_MINUTES)

    client = await _get_temporal()

    async with AsyncSessionLocal() as db:
        # Fast path: OpenClaw finished but worker died before Slack notify.
        orphan_candidates = await db.execute(
            select(Task).where(Task.status.in_(["running", "created"]))
        )
        for task in orphan_candidates.scalars().all():
            await _recover_orphaned_session_reply(client, db, task, now, stats)

        stuck_result = await db.execute(
            select(Task).where(
                Task.status.in_(["running", "created"]),
                Task.updated_at < repair_threshold,
            )
        )
        for task in stuck_result.scalars().all():
            await _repair_stuck_running_task(client, db, task, now, stats)

        result = await db.execute(
            select(Task).where(
                Task.status.in_(["running", "pending_user_input", "created"]),
                Task.updated_at < threshold,
            )
        )
        stale_tasks = result.scalars().all()
        stats["stale_tasks"] = len(stale_tasks)

        for task in stale_tasks:
            if task.status == "running" and task.updated_at < repair_threshold:
                continue
            metrics_inc("stale_detected")
            wf_id = f"workflow-{task.id}"
            handle = client.get_workflow_handle(wf_id)

            try:
                desc = await handle.describe()
                if desc.status == WorkflowExecutionStatus.COMPLETED:
                    task.status = "completed"
                    task.next_check_at = None
                    pr = await db.execute(
                        select(ProcessRun).where(ProcessRun.task_id == task.id)
                    )
                    for run in pr.scalars().all():
                        if run.current_state not in (
                            "completed",
                            "failed_terminal",
                            "stopped_by_user",
                            "compensated",
                        ):
                            run.current_state = "completed"
                            run.ended_at = now
                            run.lease_owner = None
                    stats["repaired"] += 1
                    await _cleanup_orphan_plan_children(client, task.id)
                    db.add(
                        Event(
                            correlation_id=task.correlation_id or task.id,
                            entity_type="task",
                            entity_id=task.id,
                            event_type="reconciler.repaired_completed",
                            event_payload={"workflow_status": str(desc.status)},
                        )
                    )
                    stats["events"] += 1
                    await _notify_repair(
                        task,
                        f"Task {task.id[:8]} was stale but workflow completed — status repaired to completed.",
                    )
                    continue
                if desc.status in (
                    WorkflowExecutionStatus.FAILED,
                    WorkflowExecutionStatus.TERMINATED,
                    WorkflowExecutionStatus.CANCELED,
                ):
                    terminal = (
                        "stopped_by_user"
                        if desc.status == WorkflowExecutionStatus.TERMINATED
                        else "failed"
                    )
                    task.status = terminal
                    task.next_check_at = None
                    pr = await db.execute(
                        select(ProcessRun).where(ProcessRun.task_id == task.id)
                    )
                    proc_state = (
                        "stopped_by_user"
                        if terminal == "stopped_by_user"
                        else "failed_terminal"
                    )
                    for run in pr.scalars().all():
                        if run.current_state not in (
                            "completed",
                            "failed_terminal",
                            "stopped_by_user",
                            "compensated",
                        ):
                            run.current_state = proc_state
                            run.ended_at = now
                            run.lease_owner = None
                    stats["repaired"] += 1
                    await _cleanup_orphan_plan_children(client, task.id)
                    db.add(
                        Event(
                            correlation_id=task.correlation_id or task.id,
                            entity_type="task",
                            entity_id=task.id,
                            event_type="reconciler.repaired_terminal",
                            event_payload={"workflow_status": str(desc.status)},
                        )
                    )
                    stats["events"] += 1
                    await _notify_repair(
                        task,
                        f"Task {task.id[:8]} was stale — workflow ended ({desc.status.name}); status updated.",
                    )
                    continue
            except Exception as e:
                logger.debug("Workflow describe failed for %s: %s", task.id, e)

            if task.status == "pending_user_input":
                task.next_check_at = now + timedelta(minutes=5)
                stats["events"] += 1
                continue

            task.next_check_at = now + timedelta(minutes=5)
            db.add(
                Event(
                    correlation_id=task.correlation_id or task.id,
                    entity_type="task",
                    entity_id=task.id,
                    event_type="reconciler.stale_detected",
                    event_payload={
                        "status": task.status,
                        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
                    },
                )
            )
            stats["events"] += 1
            if not _task_is_internal(task):
                await _notify_repair(
                    task,
                    f"Task {task.id[:8]} appears stalled ({task.status}). Reconciler nudged the workflow — reply here if you need changes.",
                )
                await send_alert(
                    "reconciler.stale_detected",
                    f"Stale task {task.id[:8]} ({task.status}) — no update since threshold",
                    severity="warning",
                    context={
                        "task_id": task.id,
                        "status": task.status,
                        "goal": (task.goal or "")[:200],
                        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
                    },
                )

            try:
                await handle.signal("user_input", "[RECONCILER] Continue or report status.")
                stats["re_signaled"] += 1
            except Exception as e:
                logger.warning("Could not signal stale task %s: %s", task.id, e)

        pr_result = await db.execute(
            select(ProcessRun).where(
                ProcessRun.current_state.notin_(
                    ("completed", "failed_terminal", "stopped_by_user", "canceled", "compensated")
                ),
                ProcessRun.next_check_at < now,
            )
        )
        for run in pr_result.scalars().all():
            run.next_check_at = now + timedelta(minutes=5)
            if run.current_state in ("waiting_external", "blocked"):
                try:
                    handle = client.get_workflow_handle(f"workflow-{run.task_id}")
                    await handle.signal("user_input", "[RECONCILER] External wait check-in.")
                    stats["re_signaled"] += 1
                except Exception as e:
                    logger.debug("Process run signal failed: %s", e)
            db.add(
                Event(
                    correlation_id=run.task_id,
                    entity_type="process_run",
                    entity_id=run.id,
                    event_type="reconciler.process_check",
                    event_payload={"state": run.current_state},
                )
            )
            stats["events"] += 1

        await db.commit()

    return stats


async def reconciler_loop(stop_event: asyncio.Event):
    logger.info("Reconciler started (interval=%ss)", RECONCILE_INTERVAL_SEC)
    while not stop_event.is_set():
        try:
            stats = await reconcile_once()
            if stats.get("skipped"):
                pass
            elif stats.get("stale_tasks") or stats.get("re_signaled") or stats.get("repaired"):
                logger.info("Reconciler pass: %s", stats)
        except Exception as e:
            logger.exception("Reconciler error: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=RECONCILE_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass
