"""Spawn a new process run on an existing durable/recurrent task."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.activities.db_activities import ensure_process_run
from app.db.database import AsyncSessionLocal
from app.db.models import ProcessRun, Task
from app.temporal_control import (
    signal_spawn_leg,
    start_task_workflow,
    workflow_is_running,
)

logger = logging.getLogger("rmp.task_registry.spawn")

_TERMINAL_PROCESS = frozenset(
    {"completed", "failed", "failed_terminal", "canceled", "compensated", "superseded"}
)
_ACTIVE_TASK = frozenset({"created", "running", "pending_user_input", "blocked"})


async def spawn_process_for_task(
    task_id: str,
    *,
    process_type: Optional[str] = None,
    db: Optional[AsyncSession] = None,
    start_workflow: bool = True,
    parent_task_id: Optional[str] = None,
    leg_intent: Optional[str] = None,
) -> Dict[str, Any]:
    async def _run(session: AsyncSession) -> Dict[str, Any]:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            return {"error": "task_not_found", "task_id": task_id}

        if parent_task_id and not task.parent_task_id:
            task.parent_task_id = parent_task_id

        pr_result = await session.execute(
            select(ProcessRun)
            .where(ProcessRun.task_id == task_id)
            .order_by(ProcessRun.started_at.desc())
            .limit(1)
        )
        latest = pr_result.scalar_one_or_none()
        ptype = process_type or task.task_type or "generic_task"
        is_durable = (task.task_kind or "") == "durable"

        if latest and latest.current_state not in _TERMINAL_PROCESS:
            if is_durable and await workflow_is_running(task_id):
                process_run_id = await ensure_process_run(
                    {
                        "task_id": task_id,
                        "process_type": ptype,
                        "force_new": True,
                        "durable_leg": True,
                    }
                )
                signaled = await signal_spawn_leg(
                    task_id,
                    process_run_id,
                    leg_intent or task.goal or "",
                )
                return {
                    "task_id": task_id,
                    "process_run_id": process_run_id,
                    "spawned": True,
                    "workflow_started": False,
                    "spawn_leg_signaled": signaled,
                }
            return {
                "task_id": task_id,
                "process_run_id": latest.id,
                "spawned": False,
                "reason": "active_process_exists",
            }

        process_run_id = await ensure_process_run(
            {
                "task_id": task_id,
                "process_type": ptype,
                "force_new": True,
            }
        )

        if task.status not in _ACTIVE_TASK:
            task.status = "running"
            task.next_check_at = None

        workflow_started = False
        spawn_leg_signaled = False
        if start_workflow:
            if is_durable and await workflow_is_running(task_id):
                spawn_leg_signaled = await signal_spawn_leg(
                    task_id,
                    process_run_id,
                    leg_intent or task.goal or "",
                )
            elif not await workflow_is_running(task_id):
                try:
                    await start_task_workflow(
                        task_id,
                        leg_intent or task.goal or "",
                        task.openclaw_session_key or "",
                        task.task_type or "user",
                        correlation_id=task.correlation_id or task_id,
                        process_type=ptype,
                        task_kind=task.task_kind or "one_shot",
                    )
                    workflow_started = True
                except Exception as exc:
                    logger.warning(
                        "Spawn workflow start failed for %s: %s", task_id[:8], exc
                    )
                    return {
                        "task_id": task_id,
                        "process_run_id": process_run_id,
                        "spawned": True,
                        "workflow_started": False,
                        "error": str(exc)[:200],
                    }

        return {
            "task_id": task_id,
            "process_run_id": process_run_id,
            "spawned": True,
            "workflow_started": workflow_started,
            "spawn_leg_signaled": spawn_leg_signaled,
        }

    if db is not None:
        return await _run(db)
    async with AsyncSessionLocal() as session:
        out = await _run(session)
        await session.commit()
        return out
