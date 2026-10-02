"""Temporal workflow lifecycle helpers (shared by API and task registry)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from temporalio.client import Client, WorkflowExecutionStatus

from app.telemetry import get_temporal_client_kwargs

logger = logging.getLogger("rmp.temporal_control")

TEMPORAL_ADDR = "localhost:7233"


async def connect_temporal() -> Client:
    return await Client.connect(TEMPORAL_ADDR, **get_temporal_client_kwargs())


async def connect_temporal_with_retry(
    *,
    attempts: int = 8,
    delay_sec: float = 1.0,
) -> Client:
    last: Exception | None = None
    wait = delay_sec
    for i in range(1, attempts + 1):
        try:
            return await Client.connect(TEMPORAL_ADDR, **get_temporal_client_kwargs())
        except Exception as exc:
            last = exc
            logger.warning("Temporal connect %s/%s failed: %s", i, attempts, exc)
            if i < attempts:
                await asyncio.sleep(wait)
                wait = min(wait * 2, 8.0)
    assert last is not None
    raise last


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
