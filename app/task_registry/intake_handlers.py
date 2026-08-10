"""Execute intake decisions from POST /tasks."""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, Task, TaskRegistryEntry
from app.metrics import inc as metrics_inc
from app.task_registry.intake_audit import record_intake_decision
from app.task_registry.messages import add_task_message

logger = logging.getLogger("rmp.intake_handlers")

TERMINAL = frozenset(
    {"completed", "failed", "stopped_by_user", "cancelled", "compensated"}
)


async def handle_intake_outcome(
    decision: Dict[str, Any],
    *,
    request,
    db: AsyncSession,
    intent: str,
    session_key: str,
    tags: list,
) -> Optional[Dict[str, Any]]:
    effective = decision.get("effective_decision") or "create_fresh"
    mode = decision.get("intake_mode", "shadow")
    exec_mode = decision.get("execution_mode")
    llm_raw = dict(decision.get("llm_raw") or {})
    if exec_mode:
        llm_raw["execution_mode"] = exec_mode

    decision_id = await record_intake_decision(
        request_hash=decision.get("request_hash", ""),
        decision=decision.get("decision", "create_fresh"),
        confidence=int(decision.get("confidence") or 0),
        rationale=decision.get("rationale", ""),
        similar_task_ids=decision.get("similar_task_ids"),
        llm_raw=llm_raw,
        policy_overrides=decision.get("policy_overrides"),
        intake_mode=mode,
        session_key=session_key,
        intent_snippet=intent[:500],
        db=db,
    )
    metrics_inc("intake_decided")

    db.add(
        Event(
            correlation_id=decision_id,
            entity_type="intake",
            entity_id=decision_id,
            event_type="intake.decided",
            event_payload={
                "decision": decision.get("decision"),
                "effective_decision": effective,
                "mode": mode,
                "target_task_id": decision.get("target_task_id"),
                "execution_mode": exec_mode,
            },
        )
    )

    if effective == "wait_active":
        tid = decision.get("target_task_id")
        metrics_inc("intake_wait")
        return {
            "task_id": tid,
            "status": "running",
            "intake_action": "wait_active",
            "intake_decision_id": decision_id,
            "deduplicated": True,
        }

    if effective == "attach_active":
        tid = decision.get("target_task_id")
        if tid:
            await add_task_message(
                tid, intent, role="user", source="slack", db=db
            )
            metrics_inc("intake_attached")
            return {
                "task_id": tid,
                "status": "running",
                "intake_action": "attach_active",
                "intake_decision_id": decision_id,
                "deduplicated": True,
                "signal_required": True,
            }

    if effective in ("skip_valid", "skip_noop"):
        metrics_inc("intake_skipped")
        return {
            "task_id": None,
            "status": "skipped",
            "intake_action": effective,
            "intake_decision_id": decision_id,
            "reason": decision.get("rationale"),
            "skipped": True,
        }

    if effective == "supersede":
        tid = decision.get("target_task_id")
        if tid:
            from app.temporal_control import terminate_task_workflow

            await terminate_task_workflow(tid, "superseded by intake")
            result = await db.execute(select(Task).where(Task.id == tid))
            old = result.scalar_one_or_none()
            if old and old.status not in TERMINAL:
                old.status = "failed"
                old.next_check_at = None
        return None  # fall through to create

    if effective == "spawn_process":
        tid = decision.get("target_task_id")
        if tid:
            from app.task_registry.spawn import spawn_process_for_task

            result = await db.execute(select(Task).where(Task.id == tid))
            task = result.scalar_one_or_none()
            proc = await spawn_process_for_task(
                tid,
                process_type=(task.task_type if task else None) or "generic_task",
                leg_intent=intent,
                db=db,
            )
            metrics_inc("intake_spawn")
            return {
                "task_id": tid,
                "status": "running",
                "intake_action": "spawn_process",
                "process_run_id": proc.get("process_run_id"),
                "intake_decision_id": decision_id,
                "spawned": proc.get("spawned", True),
                "workflow_started": proc.get("workflow_started", False),
            }

    if effective == "create_guided":
        notes = decision.get("guidance_notes") or decision.get("rationale") or ""
        similar = decision.get("similar_task_ids") or []
        history_lines: list[str] = []
        if similar:
            for sid in similar[:3]:
                row = await db.execute(
                    select(TaskRegistryEntry).where(TaskRegistryEntry.task_id == sid)
                )
                entry = row.scalar_one_or_none()
                if entry and entry.outcome_summary:
                    history_lines.append(
                        f"- Task {sid[:8]}: {entry.outcome_summary[:400]}"
                    )
        block_parts = []
        if history_lines:
            block_parts.append("SIMILAR COMPLETED TASKS:\n" + "\n".join(history_lines))
        if notes:
            block_parts.append(f"GUIDANCE:\n{notes[:2000]}")
        return {
            "_guided_memory_block": "\n\n".join(block_parts) or f"HISTORICAL GUIDANCE:\n{notes[:2000]}",
            "intake_decision_id": decision_id,
            "execution_mode": decision.get("execution_mode"),
        }

    return {
        "intake_decision_id": decision_id,
        "execution_mode": decision.get("execution_mode"),
    }
