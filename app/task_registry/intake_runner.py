"""Run classify_task_intake via IntakeWorkflow on the worker (never OpenClaw from API)."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import timedelta
from typing import Any, Dict

from temporalio.common import WorkflowIDReusePolicy

from app.config import get_intake_timeout_budget
from app.metrics import record_intake_run
from app.temporal_control import connect_temporal

logger = logging.getLogger("rmp.intake_runner")


def _intake_fingerprint(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "session_key": payload.get("session_key"),
                "recurrence_key": payload.get("recurrence_key"),
                "intent": (payload.get("intent") or "")[:500],
                "tags": sorted(t.lower() for t in (payload.get("tags") or [])),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:24]


async def run_classify_task_intake(payload: Dict[str, Any]) -> Dict[str, Any]:
    from app.activities.intake_activities import classify_task_intake_deterministic
    from app.workflows.intake_workflow import IntakeWorkflow

    fp = _intake_fingerprint(payload)
    budget = get_intake_timeout_budget()
    started = time.monotonic()
    path = "deterministic"
    wf_payload = {
        **payload,
        "_timeout_budget": budget,
    }

    try:
        client = await connect_temporal()
        result = await client.execute_workflow(
            IntakeWorkflow.run,
            wf_payload,
            id=f"intake-{fp}-{uuid.uuid4().hex[:8]}",
            task_queue="openclaw-tasks",
            execution_timeout=timedelta(seconds=budget["workflow_execution_sec"]),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
        if isinstance(result, dict):
            path = "workflow"
            latency_ms = int((time.monotonic() - started) * 1000)
            record_intake_run(
                path=path,
                latency_ms=latency_ms,
                confidence=int(result.get("confidence") or 0),
            )
            return result
    except Exception as exc:
        logger.warning("IntakeWorkflow failed, deterministic fallback: %s", exc)

    result = await classify_task_intake_deterministic(payload)
    latency_ms = int((time.monotonic() - started) * 1000)
    record_intake_run(
        path=path,
        latency_ms=latency_ms,
        confidence=int(result.get("confidence") or 0),
    )
    return result
