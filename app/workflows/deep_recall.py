"""The deep recall child workflow (``{task_id}-recall``): plan, retrieve, read, within a bounded time.

It runs beside Aura's first answer and never delays it. The parent bounds it with an execution
timeout; a failed step closes the report as failed, so the parent always learns the outcome.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from app.activities.deep_memory_activities import (
        close_recall_report,
        plan_recall_step,
        read_recall_step,
        retrieve_recall_step,
        start_recall_report,
    )

TWO_TRIES = RetryPolicy(maximum_attempts=2)


def recall_workflow_id(task_id: str) -> str:
    return f"{task_id}-recall"


@workflow.defn
class DeepRecallWorkflow:
    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        task_id = payload["task_id"]
        report_id = await workflow.execute_activity(
            start_recall_report, payload, start_to_close_timeout=timedelta(seconds=15)
        )

        async def close(status: str, reason: str) -> Dict[str, Any]:
            await workflow.execute_activity(
                close_recall_report, {"report_id": report_id, "status": status, "reason": reason},
                start_to_close_timeout=timedelta(seconds=15),
            )
            return {"status": status, "report_id": report_id, "reason": reason}

        try:
            planned = await workflow.execute_activity(
                plan_recall_step,
                {"report_id": report_id, "task_id": task_id, "query": payload.get("query") or "",
                 "session_key": payload.get("session_key") or ""},
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=TWO_TRIES,
            )
            plan = planned["plan"]
            if not plan.get("needed") or not plan.get("sub_queries"):
                return await close("skipped", plan.get("reason") or "no memory needed")
            evidence = await workflow.execute_activity(
                retrieve_recall_step,
                {"report_id": report_id, "task_id": task_id, "plan": plan},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=TWO_TRIES,
            )
            if not evidence:
                return await close("empty", "nothing in memory matched")
            return await workflow.execute_activity(
                read_recall_step,
                {"report_id": report_id, "query": payload.get("query") or "",
                 "dialogue": planned.get("dialogue") or "", "evidence": evidence},
                start_to_close_timeout=timedelta(seconds=150),
                retry_policy=TWO_TRIES,
            )
        except ActivityError as exc:
            return await close("failed", str(exc.cause or exc)[:300])
