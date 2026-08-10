"""Temporal workflow lifecycle helpers (shared by API and task registry)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from temporalio.client import Client, WorkflowExecutionStatus

from app.telemetry import get_temporal_client_kwargs
from app.workflows.catalog import catalog_type_for_workflow
from app.orchestrator.prompt_policy import resolve_generic_profile

logger = logging.getLogger("rmp.temporal_control")


async def connect_temporal() -> Client:
    return await Client.connect("localhost:7233", **get_temporal_client_kwargs())


async def terminate_task_workflow(task_id: str, reason: str = "superseded") -> bool:
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
) -> None:
    catalog_type = catalog_type_for_workflow(process_type, intent, task_type)
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
    profile = resolve_generic_profile(intent) if not catalog_type else None
    if profile:
        payload["generic_profile"] = profile
    if initial_memory_block:
        payload["initial_memory_block"] = initial_memory_block

    from app.orchestrator.completion_rework import get_rework_max_attempts
    from app.orchestrator.execution_mode import resolve_execution_mode
    from app.orchestrator.prompt_policy import user_local_time_block

    payload["rework_max_attempts"] = get_rework_max_attempts()
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
