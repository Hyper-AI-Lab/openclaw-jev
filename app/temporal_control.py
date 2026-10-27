"""Temporal workflow lifecycle helpers (shared by API and task registry)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Dict, List, Optional, Set, Tuple

from sqlalchemy import select
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app.config import TEMPORAL_ADDRESS
from app.db.database import AsyncSessionLocal
from app.db.models import Event, ProcessRun, Task
from app.status import RunState, TaskStatus
from app.telemetry import get_temporal_client_kwargs
from app.transitions import set_run_state, set_task_status

logger = logging.getLogger("rmp.temporal_control")

# The process's Temporal client for each running event loop, by id(loop): (loop, client or None, connect lock).
_clients: Dict[int, Tuple[asyncio.AbstractEventLoop, Optional[Client], asyncio.Lock]] = {}
# Work started by run_in_background, referenced until it is done.
_background: Set[asyncio.Task] = set()
# How long a cancel that waits for Aura's runs to abort (the sentinel, a short-lived process) waits.
ABORT_WAIT_SEC = 30


async def connect_temporal(*, attempts: int = 1, delay_sec: float = 1.0) -> Client:
    """The process's one Temporal client for the running event loop.

    A cached client is returned once its health check passes. Otherwise it is dropped and a new one connects to
    ``TEMPORAL_ADDRESS`` with the telemetry interceptor, tried up to ``attempts`` times with a delay that doubles from
    ``delay_sec`` up to 8 s.
    """
    loop = asyncio.get_running_loop()
    for key, (owner, _, _) in list(_clients.items()):
        if owner.is_closed() or (key == id(loop) and owner is not loop):
            _clients.pop(key, None)
    entry = _clients.get(id(loop))
    if entry is None:
        entry = _clients[id(loop)] = (loop, None, asyncio.Lock())
    _, client, lock = entry
    if client is not None:
        try:
            await client.service_client.check_health()
            return client
        except Exception as exc:
            logger.warning("Temporal client failed its health check; reconnecting: %s", exc)
    async with lock:
        current = _clients[id(loop)][1]
        if current is not None and current is not client:
            # Another caller reconnected while this one waited for the lock.
            return current
        _clients[id(loop)] = (loop, None, lock)
        last: Exception | None = None
        wait = delay_sec
        for i in range(1, attempts + 1):
            try:
                client = await Client.connect(TEMPORAL_ADDRESS, **get_temporal_client_kwargs())
            except Exception as exc:
                last = exc
                logger.warning("Temporal connect %s/%s failed: %s", i, attempts, exc)
                if i < attempts:
                    await asyncio.sleep(wait)
                    wait = min(wait * 2, 8.0)
                continue
            _clients[id(loop)] = (loop, client, lock)
            return client
        assert last is not None
        raise last


def run_in_background(work: Coroutine[Any, Any, Any], *, what: str) -> asyncio.Task:
    """Run ``work`` without waiting for it: a reference is kept until it is done, and a failure is logged."""
    task = asyncio.get_running_loop().create_task(work)
    _background.add(task)

    def done(t: asyncio.Task) -> None:
        _background.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("%s failed: %s", what, t.exception())

    task.add_done_callback(done)
    return task


async def cancel_task(task_id: str, *, reason: str, actor: str, wait_abort: bool = False) -> bool:
    """Cancel a task from outside its workflow: ``POST /tasks/{id}/cancel``, the canary preemption, the sentinel.

    The status is written first, so the compare-and-set decides whether this call is the one that cancels; a task that
    is already finished is refused by the guarded writer and nothing else happens. Only the call that moved the task to
    ``cancelled`` ends its latest run, records ``task.cancelled``, aborts Aura's runs (awaited up to ABORT_WAIT_SEC
    when ``wait_abort``) and terminates ``workflow-<id>``, whose plan children follow by their TERMINATE parent-close
    policy. Returns True only for that call.
    """
    async with AsyncSessionLocal() as db:
        cancelled = await set_task_status(db, task_id, TaskStatus.CANCELLED, actor=actor, cause=reason,
                                          closed_reason=reason)
        if cancelled.changed:
            run_id = (await db.execute(
                select(ProcessRun.id).where(ProcessRun.task_id == task_id)
                .order_by(ProcessRun.started_at.desc()).limit(1)
            )).scalar_one_or_none()
            if run_id:
                await set_run_state(db, run_id, RunState.CANCELED, actor=actor, cause=reason, ended=True,
                                    release_lease=True)
            correlation_id = (await db.execute(select(Task.correlation_id).where(Task.id == task_id))).scalar()
            db.add(Event(correlation_id=correlation_id or task_id, entity_type="task", entity_id=task_id,
                         event_type="task.cancelled", event_payload={"source": actor, "reason": reason}))
        await db.commit()
    if not cancelled.changed:
        return False

    from app.openclaw_control import abort_task_runs, schedule_abort

    if wait_abort:
        try:
            await asyncio.wait_for(abort_task_runs(task_id, reason=reason), ABORT_WAIT_SEC)
        except Exception as exc:
            logger.warning("Aborting Aura's runs for cancelled task %s: %s", task_id[:8], exc)
    else:
        schedule_abort(task_id, reason=reason)
    try:
        client = await connect_temporal()
        handle = client.get_workflow_handle(f"workflow-{task_id}")
        try:
            await handle.signal("cancel", reason)
        except Exception:
            pass
        await handle.terminate(reason)
    except RPCError as exc:
        # NOT_FOUND: the workflow is gone or already closed, which is what the terminate wanted.
        if exc.status != RPCStatusCode.NOT_FOUND:
            logger.warning("Terminate workflow %s: %s", task_id[:8], exc)
    except Exception as exc:
        logger.warning("Terminate workflow %s: %s", task_id[:8], exc)
    from app.task_registry.hooks import index_terminal_task_async

    await index_terminal_task_async(task_id)
    return True


async def terminate_task_workflow(task_id: str, reason: str = "superseded") -> bool:
    """End a task's workflow, Aura's runs for it and its coding units (rebuild_stale and supersede)."""
    from app.activities.coding_activities import stop_task_units
    from app.openclaw_control import schedule_abort

    schedule_abort(task_id, reason=reason)
    try:
        await asyncio.to_thread(stop_task_units, task_id)
    except Exception as exc:
        logger.warning("Stopping coding units of %s: %s", task_id[:8], exc)
    try:
        client = await connect_temporal()
        handle = client.get_workflow_handle(f"workflow-{task_id}")
        try:
            await handle.signal("cancel", reason)
        except Exception:
            pass
        await handle.terminate(reason)
        return True
    except Exception as exc:
        logger.warning("Terminate workflow %s: %s", task_id[:8], exc)
        return False


async def workflow_is_running(task_id: str) -> bool:
    try:
        client = await connect_temporal()
        handle = client.get_workflow_handle(f"workflow-{task_id}")
        desc = await handle.describe()
        return desc.status == WorkflowExecutionStatus.RUNNING
    except Exception:
        return False


async def start_task_workflow(
    task_id: str,
    intent: str,
    session_key: str,
    task_type: str,
    *,
    correlation_id: Optional[str] = None,
    workflow_name: Optional[str] = None,
    process_type: Optional[str] = None,
    tags: Optional[List[str]] = None,
    initial_memory_block: Optional[str] = None,
    task_kind: Optional[str] = None,
    execution_mode: Optional[str] = None,
    recall_depth: Optional[str] = None,
) -> None:
    from app.workflows.catalog import CATALOG, CATALOG_ALIASES, get_template, normalize_catalog_type

    # Catalog workflows are assigned by intake LLM (or explicit API process_type).
    # Do NOT re-derive from intent keyword patterns — that bypasses adjudication.
    catalog_type: Optional[str] = None
    explicit = process_type or (
        task_type if (task_type in CATALOG or task_type in CATALOG_ALIASES) else None
    )
    if explicit:
        catalog_type = normalize_catalog_type(str(explicit), "")
        if catalog_type and not get_template(catalog_type):
            catalog_type = None

    if workflow_name is None and "coding_task" in (process_type, task_type, catalog_type):
        workflow_name = "CodingTaskWorkflow"
    if workflow_name is None:
        workflow_name = "CatalogTaskWorkflow" if catalog_type else "GenericTaskWorkflow"

    payload: Dict[str, Any] = {
        "task_id": task_id,
        "intent": intent,
        "session_key": session_key,
        "correlation_id": correlation_id or task_id,
        "task_type": task_type,
        "tags": tags or [],
        "task_kind": task_kind or "one_shot",
    }
    if catalog_type:
        payload["process_type"] = catalog_type
    if initial_memory_block:
        payload["initial_memory_block"] = initial_memory_block
    if recall_depth:
        payload["recall_depth"] = recall_depth
    from app.config import get_deep_memory_config
    from app.notification_policy import is_internal_task

    deep = get_deep_memory_config()
    payload["deep_recall"] = {
        "enabled": bool(deep.get("recall_enabled")) and recall_depth != "none"
        and not is_internal_task(intent, task_type, tags or []),
        "followups": bool(deep.get("followups_enabled")),
        "deadline_sec": int(deep.get("recall_deadline_sec") or 180),
        "wait_sec": int(deep.get("followup_wait_sec") or 300),
    }

    from app.orchestrator.completion_rework import get_attempt_policy
    from app.orchestrator.execution_mode import resolve_execution_mode
    from app.orchestrator.prompt_policy import user_local_time_block

    policy = get_attempt_policy()
    payload["rework_max_attempts"] = policy["max_attempts"]
    payload["strategy_change_attempt"] = policy["strategy_change_attempt"]
    payload["escalate_user_attempt"] = policy["escalate_user_attempt"]
    payload["user_time_block"] = user_local_time_block()
    payload["execution_mode"] = resolve_execution_mode(
        intent=intent,
        tags=tags,
        task_type=task_type,
        llm_mode=execution_mode,
        catalog_type=catalog_type,
    )

    client = await connect_temporal()
    await client.start_workflow(
        workflow_name,
        payload,
        id=f"workflow-{task_id}",
        task_queue="openclaw-tasks",
    )


async def signal_spawn_leg(
    task_id: str,
    process_run_id: str,
    intent: str = "",
) -> bool:
    try:
        client = await connect_temporal()
        handle = client.get_workflow_handle(f"workflow-{task_id}")
        await handle.signal(
            "spawn_leg",
            {"process_run_id": process_run_id, "intent": intent},
        )
        return True
    except Exception as exc:
        logger.warning("spawn_leg signal failed for %s: %s", task_id[:8], exc)
        return False
