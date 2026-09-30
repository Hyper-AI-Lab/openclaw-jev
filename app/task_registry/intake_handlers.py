"""Execute intake decisions from POST /tasks."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, Observation, ProcessRun, Task, TaskRegistryEntry
from app.metrics import inc as metrics_inc
from app.orchestrator.process_brief import with_catchup
from app.task_registry.intake_audit import record_intake_decision
from app.task_registry.messages import add_task_message

logger = logging.getLogger("rmp.intake_handlers")

TERMINAL = frozenset(
    {"completed", "failed", "stopped_by_user", "cancelled", "compensated"}
)
_SILENT_TAGS = frozenset({"canary", "system", "heartbeat", "intake-smoke"})


async def acknowledge_attach(
    *, session_key: str, task_ids: list, intent: str, tags: Optional[list] = None
) -> None:
    """RMP note that a message joined running work. Quotes the message so each note is distinct."""
    snippet = " ".join((intent or "").split())
    snippet = snippet if len(snippet) <= 60 else snippet[:57] + "..."
    shorts = ", ".join(t[:8] for t in task_ids)
    where = f"the task I'm working on ({shorts})" if len(task_ids) == 1 else f"the tasks I'm working on ({shorts})"
    await _intake_notify_slack(
        session_key=session_key,
        task_id=task_ids[0],
        message=f"Got it: adding \u201c{snippet}\u201d to {where}.",
        intent=intent,
        tags=tags,
    )


async def _intake_notify_slack(
    *,
    session_key: str,
    task_id: str,
    message: str,
    intent: str = "",
    task_type: str = "user",
    tags: Optional[list] = None,
) -> None:
    tag_set = {str(t).lower() for t in (tags or [])}
    if tag_set & _SILENT_TAGS:
        return
    if not message or not session_key:
        return
    try:
        from app.activities.openclaw_activities import notify_slack_user

        await notify_slack_user(
            {
                "session_key": session_key,
                "task_id": task_id,
                "message": message,
                "intent": intent,
                "task_type": task_type,
                "tags": tags or ["user-request"],
            }
        )
    except Exception as exc:
        logger.warning("Intake Slack notify failed for %s: %s", task_id, exc)


def _requester(request) -> str:
    return getattr(request, "user_id", None) or "slack_user"


def _slack_meta(request) -> Optional[dict]:
    """Where the message sits in Slack: its thread, the message it replies to, its files."""
    message_id = getattr(request, "slack_message_id", None)
    attachments = getattr(request, "attachments", None) or []
    if not message_id and not attachments:
        return None
    return {
        "message_id": message_id,
        "thread_id": getattr(request, "thread_id", None),
        "reply_to_id": (getattr(request, "reply_to", None) or {}).get("id"),
        "attachments": attachments,
    }


async def build_catchup_block(task_id: str, db: AsyncSession) -> str:
    """Process memory + last steps for attach/rebuild. Fail-soft."""
    lines = [f"PROCESS BRIEF: continue task {task_id}"]
    try:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task:
            lines.append(f"status={task.status} type={task.task_type} goal={(task.goal or '')[:400]}")
            ctx = task.supplementary_context or {}
            if ctx.get("clarify_question"):
                lines.append(f"prior clarify question: {ctx.get('clarify_question')}")
            if ctx.get("original_intent"):
                lines.append(f"original ask: {str(ctx.get('original_intent'))[:800]}")
        reg = await db.execute(
            select(TaskRegistryEntry).where(TaskRegistryEntry.task_id == task_id)
        )
        entry = reg.scalar_one_or_none()
        if entry and entry.outcome_summary:
            lines.append(f"registry outcome: {entry.outcome_summary[:500]}")
        pr = await db.execute(
            select(ProcessRun)
            .where(ProcessRun.task_id == task_id)
            .order_by(ProcessRun.started_at.desc())
        )
        run = pr.scalars().first()
        if run:
            lines.append(f"process_run={run.id} state={run.current_state} type={run.process_type}")
            obs = await db.execute(
                select(Observation)
                .where(Observation.process_run_id == run.id)
                .order_by(Observation.observed_at.desc())
                .limit(5)
            )
            for o in obs.scalars().all():
                payload = o.payload_ref if isinstance(o.payload_ref, dict) else {}
                snippet = (
                    payload.get("text")
                    or payload.get("summary")
                    or payload.get("reason")
                    or str(payload)[:200]
                )
                lines.append(f"obs {o.observation_type or o.source}: {str(snippet)[:300]}")
    except Exception as exc:
        logger.debug("Catch-up block failed for %s: %s", task_id, exc)
        lines.append("(catch-up metadata unavailable)")
    return "\n".join(lines)


async def maybe_remind_intake_clarify(task: Task, now: datetime) -> bool:
    """One Slack reminder for unanswered intake clarify; then wait."""
    ctx = dict(task.supplementary_context or {})
    if not ctx.get("intake_clarify"):
        return False
    if ctx.get("clarify_reminded"):
        return False
    question = ctx.get("clarify_question") or "Still need a bit more detail before I start."
    await _intake_notify_slack(
        session_key=task.openclaw_session_key or "",
        task_id=task.id,
        message=f"Still waiting on this before I start:\n{question}",
        intent=task.goal or "",
        task_type=task.task_type or "user",
        tags=["user-request"],
    )
    ctx["clarify_reminded"] = True
    task.supplementary_context = ctx
    return True


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
    if decision.get("recall_depth"):
        llm_raw["recall_depth"] = decision["recall_depth"]
    relation_class = decision.get("relation_class") or ""

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
                "relation_class": relation_class,
                "recall_depth": decision.get("recall_depth"),
            },
        )
    )

    if effective == "wait_active":
        tid = decision.get("target_task_id")
        metrics_inc("intake_wait")
        short = (tid or "")[:8]
        await _intake_notify_slack(
            session_key=session_key,
            task_id=tid or decision_id,
            message=(
                f"I'm already working on this (task {short}). "
                "I'll continue there instead of starting a second run."
            ),
            intent=intent,
            tags=tags,
        )
        db.add(
            Event(
                correlation_id=tid or decision_id,
                entity_type="task",
                entity_id=tid or decision_id,
                event_type="intake.wait_active",
                event_payload={"target_task_id": tid, "decision_id": decision_id},
            )
        )
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
            targets = [tid] + [t for t in decision.get("target_task_ids") or [] if t != tid]
            result = await db.execute(select(Task).where(Task.id == tid))
            existing = result.scalar_one_or_none()
            raw_ctx = (
                getattr(existing, "supplementary_context", None)
                if existing is not None
                else None
            )
            ctx = dict(raw_ctx) if isinstance(raw_ctx, dict) else {}
            intake_clarify = bool(ctx.get("intake_clarify"))
            if intake_clarify:
                targets = [tid]
            signals = []
            for target in targets:
                catchup = await build_catchup_block(target, db)
                await add_task_message(
                    target, intent, role="user", source="slack", db=db,
                    slack_ts=getattr(request, "slack_message_id", None),
                    kind="clarify_answer" if intake_clarify else "attached",
                    session_key=session_key,
                    meta={
                        "slack": _slack_meta(request),
                        "intake_decision_id": decision_id,
                        "targets": targets if len(targets) > 1 else None,
                    },
                )
                metrics_inc("intake_attached")
                db.add(
                    Event(
                        correlation_id=target,
                        entity_type="task",
                        entity_id=target,
                        event_type="intake.attach",
                        event_payload={
                            "decision_id": decision_id,
                            "resume_clarify": intake_clarify,
                            "targets": targets,
                        },
                    )
                )
                signals.append(
                    {
                        "task_id": target,
                        "catchup": catchup,
                        "signal_text": with_catchup(catchup, intent),
                    }
                )
            catchup = signals[0]["catchup"]
            if existing and intake_clarify:
                existing.status = "created"
                ctx["intake_clarify"] = False
                ctx["answered_at"] = datetime.utcnow().isoformat()
                existing.supplementary_context = ctx
                combined = (
                    f"{catchup}\n\nCLARIFY ANSWER:\n{intent}\n\n"
                    f"ORIGINAL ASK:\n{ctx.get('original_intent') or existing.goal or ''}"
                )
                return {
                    "task_id": tid,
                    "status": "created",
                    "intake_action": "resume_clarify",
                    "intake_decision_id": decision_id,
                    "execution_mode": exec_mode,
                    "_guided_memory_block": combined,
                    "session_key": session_key,
                    "task_type": (existing.task_type if existing else None) or "user",
                    "signal_required": False,
                }
            return {
                "task_id": tid,
                "target_task_ids": targets,
                "status": "running",
                "intake_action": "attach_active",
                "intake_decision_id": decision_id,
                "deduplicated": True,
                "signal_required": True,
                "signal_text": signals[0]["signal_text"],
                "signals": [{"task_id": s["task_id"], "signal_text": s["signal_text"]} for s in signals],
                "_guided_memory_block": catchup,
            }

    if effective == "clarify":
        question = (
            decision.get("guidance_notes")
            or decision.get("rationale")
            or "Could you clarify what you'd like me to do?"
        )
        task_id = str(uuid.uuid4())
        now = datetime.utcnow()
        task = Task(
            id=task_id,
            correlation_id=task_id,
            idempotency_key=str(uuid.uuid4()),
            requester=_requester(request),
            openclaw_session_key=session_key,
            task_type="user",
            goal=intent,
            status="pending_user_input",
            task_kind="one_shot",
            intake_decision_id=decision_id,
            next_check_at=now + timedelta(minutes=30),
            supplementary_context={
                "intake_clarify": True,
                "clarify_question": question[:2000],
                "original_intent": intent[:8000],
                "relation_class": relation_class or "new",
            },
        )
        db.add(task)
        db.add(
            Event(
                correlation_id=task_id,
                entity_type="task",
                entity_id=task_id,
                event_type="intake.clarify",
                event_payload={"decision_id": decision_id, "question": question[:500]},
            )
        )
        await add_task_message(
            task_id, intent, role="user", source="slack", db=db,
            slack_ts=getattr(request, "slack_message_id", None),
            kind="request",
            session_key=session_key,
            meta={"slack": _slack_meta(request), "intake_decision_id": decision_id, "clarify": True},
        )
        await _intake_notify_slack(
            session_key=session_key,
            task_id=task_id,
            message=question[:3500],
            intent=intent,
            tags=tags,
        )
        return {
            "task_id": task_id,
            "status": "pending_user_input",
            "intake_action": "clarify",
            "intake_decision_id": decision_id,
            "workflow_started": False,
        }

    if effective in ("skip_valid", "skip_noop"):
        metrics_inc("intake_skipped")
        ack_id = str(uuid.uuid4())
        rationale = decision.get("rationale") or "Nothing new to run."
        ack = (
            "Got it — nothing new to run. I'll stay with the last result."
            if effective == "skip_valid"
            else "Noted. No action needed on my side."
        )
        if rationale and rationale not in ack:
            ack = f"{ack}\n{rationale[:400]}"
        task = Task(
            id=ack_id,
            correlation_id=ack_id,
            idempotency_key=str(uuid.uuid4()),
            requester=_requester(request),
            openclaw_session_key=session_key,
            task_type="user",
            goal=intent,
            status="completed",
            task_kind="one_shot",
            intake_decision_id=decision_id,
            supplementary_context={"intake_ack": True, "skip_kind": effective},
        )
        db.add(task)
        db.add(
            Event(
                correlation_id=ack_id,
                entity_type="task",
                entity_id=ack_id,
                event_type="intake.skip_ack",
                event_payload={"decision_id": decision_id, "kind": effective},
            )
        )
        await add_task_message(
            ack_id, intent, role="user", source="slack", db=db,
            slack_ts=getattr(request, "slack_message_id", None),
            kind="request",
            session_key=session_key,
            meta={"slack": _slack_meta(request), "intake_decision_id": decision_id, "skip_kind": effective},
        )
        await _intake_notify_slack(
            session_key=session_key,
            task_id=ack_id,
            message=ack[:3500],
            intent=intent,
            tags=tags,
        )
        return {
            "task_id": ack_id,
            "status": "completed",
            "intake_action": effective,
            "intake_decision_id": decision_id,
            "reason": rationale,
            "skipped": True,
        }

    if effective == "rebuild_stale":
        tid = decision.get("target_task_id")
        catchup = ""
        if tid:
            catchup = await build_catchup_block(tid, db)
            from app.temporal_control import terminate_task_workflow

            await terminate_task_workflow(tid, "rebuild_stale by intake")
            result = await db.execute(select(Task).where(Task.id == tid))
            old = result.scalar_one_or_none()
            if old and old.status not in TERMINAL:
                old.status = "failed"
                old.next_check_at = None
            db.add(
                Event(
                    correlation_id=tid,
                    entity_type="task",
                    entity_id=tid,
                    event_type="intake.rebuild_stale",
                    event_payload={"decision_id": decision_id, "superseded_task_id": tid},
                )
            )
        notes = decision.get("guidance_notes") or decision.get("rationale") or ""
        block = "\n\n".join(
            p
            for p in (
                f"PROCESS BRIEF: rebuild_stale of task {tid or '?'}. This is a new run with prior process memory.",
                catchup,
                f"NEW USER MESSAGE:\n{intent}",
                f"GUIDANCE:\n{notes[:2000]}" if notes else "",
            )
            if p
        )
        return {
            "_guided_memory_block": block,
            "intake_decision_id": decision_id,
            "execution_mode": exec_mode,
            "relation_class": "running",
            "_relation_marker": "rebuild_stale",
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
        block_parts = [
            "PROCESS BRIEF: related to finished work (create_guided). This is not a brand-new topic.",
        ]
        if history_lines:
            block_parts.append("SIMILAR COMPLETED TASKS:\n" + "\n".join(history_lines))
        if notes:
            block_parts.append(f"GUIDANCE:\n{notes[:2000]}")
        return {
            "_guided_memory_block": "\n\n".join(block_parts) or f"HISTORICAL GUIDANCE:\n{notes[:2000]}",
            "intake_decision_id": decision_id,
            "execution_mode": decision.get("execution_mode"),
            "relation_class": relation_class or "finished",
        }

    fresh_brief = (
        "PROCESS BRIEF: new RMP task for this Slack message in the same conversation. "
        "Use RECENT DIALOGUE when present. Do not continue canary/system jobs. "
        "create_fresh means a new task row, not amnesia."
        if effective == "create_fresh"
        else ""
    )
    return {
        "intake_decision_id": decision_id,
        "execution_mode": decision.get("execution_mode"),
        "relation_class": relation_class or "new",
        "_guided_memory_block": fresh_brief,
        "_relation_marker": "new" if effective == "create_fresh" else effective,
    }
