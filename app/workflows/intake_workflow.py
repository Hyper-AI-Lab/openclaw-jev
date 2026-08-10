"""Short-lived workflow to run task intake on the worker (OpenClaw LLM requires activity context)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy


with workflow.unsafe.imports_passed_through():
    from app.activities.intake_activities import classify_task_intake_activity
    from app.task_registry.intake_timeouts import intake_timeout_budget


def _budget_from_payload(payload: Dict[str, Any]) -> dict:
    raw = payload.get("_timeout_budget")
    if isinstance(raw, dict) and raw.get("llm_sec"):
        return raw
    return intake_timeout_budget()


@workflow.defn
class IntakeWorkflow:
    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        budget = _budget_from_payload(payload)
        return await workflow.execute_activity(
            classify_task_intake_activity,
            payload,
            start_to_close_timeout=timedelta(
                seconds=budget["activity_start_to_close_sec"]
            ),
            heartbeat_timeout=timedelta(seconds=budget["activity_heartbeat_sec"]),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
