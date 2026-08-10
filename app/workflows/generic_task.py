from datetime import timedelta
from typing import Any, Dict, List

import re

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.activities.openclaw_activities import (
        notify_slack_user,
        send_to_openclaw,
        verify_response_quality,
    )
    from app.activities.plan_activities import generate_process_plan, save_process_plan
    from app.activities.db_activities import (
        build_process_memory_context,
        compact_episodic_memory,
        ensure_process_run,
        finalize_task_failure,
        promote_completion_memory,
        execute_compensation,
        record_event,
        update_process_state,
        update_task_status,
        write_episodic_observation,
    )
    from app.evidence import check_evidence, evidence_high_confidence
    from app.orchestrator.completion_rework import build_rework_prompt, get_rework_max_attempts
    from app.orchestrator.decision_engine import decide_completion_gate
    from app.workflows.generic_execute_child import GenericExecuteChildWorkflow
    from app.notification_policy import (
        format_workflow_error,
        is_internal_task,
        is_silent_system_ack,
        strip_system_acks,
    )


def strip_json_eval(text: str) -> str:
    cleaned = re.sub(
        r"```json\s*\{[^{}]*\"task_status\"[^{}]*\}\s*```", "", text, flags=re.DOTALL
    )
    cleaned = re.sub(r"\{[^{}]*\"task_status\"[^{}]*\}", "", cleaned).strip()
    return strip_system_acks(cleaned)


def is_heartbeat_request(text: str) -> bool:
    from app.notification_policy import is_heartbeat_request as _is_hb

    return _is_hb(text)


def is_heartbeat_ack(text: str) -> bool:
    return is_silent_system_ack(text)


@workflow.defn
class GenericTaskWorkflow:
    def __init__(self) -> None:
        self.user_inputs: List[str] = []
        self.process_run_id: str = ""
        self._cancel_requested: bool = False
        self._retry_requested: bool = False
        self._approved: bool = False
        self._spawn_leg_requested: bool = False
        self._spawn_leg_payload: Dict[str, Any] = {}

    @workflow.signal
    def user_input(self, message: str) -> None:
        self.user_inputs.append(message)

    @workflow.signal
    def spawn_leg(self, payload: Dict[str, Any]) -> None:
        self._spawn_leg_payload = payload or {}
        self._spawn_leg_requested = True

    @workflow.signal
    def cancel(self, reason: str = "") -> None:
        self._cancel_requested = True
        if reason:
            self.user_inputs.append(f"[CANCEL] {reason}")

    @workflow.signal
    def retry(self) -> None:
        self._retry_requested = True

    @workflow.signal
    def approve(self, message: str = "") -> None:
        self._approved = True
        if message:
            self.user_inputs.append(message)

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        task_id = payload.get("task_id", "unknown")
        user_intent = payload.get("intent", "")
        session_key = payload.get("session_key", "agent:main:main")
        correlation_id = payload.get("correlation_id", task_id)
        generic_profile = payload.get("generic_profile")
        user_time_block = payload.get("user_time_block") or ""
        execution_mode = payload.get("execution_mode") or ""
        task_kind = payload.get("task_kind", "one_shot")

        if is_heartbeat_request(payload.get("intent", "")):
            await workflow.execute_activity(
                compact_episodic_memory,
                {"max_age_days": 30},
                start_to_close_timeout=timedelta(seconds=60),
            )

        self.process_run_id = await workflow.execute_activity(
            ensure_process_run,
            {"task_id": task_id, "process_type": payload.get("task_type", "generic")},
            start_to_close_timeout=timedelta(seconds=15),
        )

        await workflow.execute_activity(
            record_event,
            {
                "correlation_id": correlation_id,
                "entity_type": "task",
                "entity_id": task_id,
                "event_type": "task.started",
                "event_payload": {"intent": user_intent[:500]},
            },
            start_to_close_timeout=timedelta(seconds=10),
        )

        await workflow.execute_activity(
            update_task_status,
            {"task_id": task_id, "status": "running", "next_check_minutes": 10},
            start_to_close_timeout=timedelta(seconds=10),
        )

        try:
            result = await self._plan_driven_loop(
                task_id,
                session_key,
                user_intent,
                correlation_id,
                payload.get("task_type", "generic"),
                payload.get("tags") or [],
                generic_profile,
                initial_memory_block=payload.get("initial_memory_block"),
                rework_max_attempts=int(payload.get("rework_max_attempts") or 3),
                user_time_block=user_time_block,
                execution_mode=execution_mode,
            )
        except Exception as e:
            await workflow.execute_activity(
                execute_compensation,
                {
                    "task_id": task_id,
                    "process_run_id": self.process_run_id,
                    "reason": f"Workflow exception: {str(e)[:300]}",
                },
                start_to_close_timeout=timedelta(seconds=15),
            )
            err_text = format_workflow_error(e)
            await workflow.execute_activity(
                notify_slack_user,
                {
                    "session_key": session_key,
                    "task_id": task_id,
                    "intent": user_intent,
                    "task_type": payload.get("task_type", "generic"),
                    "tags": payload.get("tags") or [],
                    "message": f"Task compensated after error: {err_text}",
                },
                start_to_close_timeout=timedelta(seconds=30),
            )
            return {"status": "compensated", "task_id": task_id, "reason": err_text}

        while task_kind == "durable" and not self._cancel_requested:
            await workflow.wait_condition(
                lambda: self._spawn_leg_requested or self._cancel_requested
            )
            if self._cancel_requested:
                return result
            if not self._spawn_leg_requested:
                return result
            leg = self._spawn_leg_payload or {}
            self._spawn_leg_requested = False
            if leg.get("process_run_id"):
                self.process_run_id = leg["process_run_id"]
            leg_intent = leg.get("intent") or user_intent
            try:
                result = await self._plan_driven_loop(
                    task_id,
                    session_key,
                    leg_intent,
                    correlation_id,
                    payload.get("task_type", "generic"),
                    payload.get("tags") or [],
                    generic_profile,
                    initial_memory_block=payload.get("initial_memory_block"),
                    rework_max_attempts=int(payload.get("rework_max_attempts") or 3),
                    user_time_block=user_time_block,
                    execution_mode=execution_mode,
                )
            except Exception as e:
                err_text = format_workflow_error(e)
                return {"status": "compensated", "task_id": task_id, "reason": err_text}

        if isinstance(result, dict) and result.get("status") in ("compensated", "failed"):
            if not is_internal_task(
                user_intent,
                payload.get("task_type", "generic"),
                payload.get("tags") or [],
            ):
                reason = result.get("reason") or result.get("status")
                await workflow.execute_activity(
                    notify_slack_user,
                    {
                        "session_key": session_key,
                        "task_id": task_id,
                        "intent": user_intent,
                        "task_type": payload.get("task_type", "generic"),
                        "tags": payload.get("tags") or [],
                        "message": (
                            f"I couldn't complete your request ({reason}). "
                            "Please try again in a moment."
                        ),
                    },
                    start_to_close_timeout=timedelta(seconds=30),
                )

        return result

    async def _plan_driven_loop(
        self,
        task_id: str,
        session_key: str,
        user_intent: str,
        correlation_id: str,
        task_type: str,
        tags: List[str],
        generic_profile: str | None,
        initial_memory_block: str | None = None,
        rework_max_attempts: int = 3,
        user_time_block: str = "",
        execution_mode: str = "",
    ) -> Dict[str, Any]:
        plan_result = await workflow.execute_activity(
            generate_process_plan,
            {
                "task_id": task_id,
                "intent": user_intent,
                "session_key": session_key,
                "task_type": task_type,
                "tags": tags,
                "execution_mode": execution_mode,
            },
            start_to_close_timeout=timedelta(minutes=5),
        )
        await workflow.execute_activity(
            save_process_plan,
            {"process_run_id": self.process_run_id, "plan": plan_result},
            start_to_close_timeout=timedelta(seconds=10),
        )
        steps = plan_result.get("steps") or []
        step_context = ""
        final_text = ""
        is_conversational = execution_mode == "conversational"
        skip_semantic = task_type == "canary" or "memory-canary" in {t.lower() for t in tags}

        memory_payload: Dict[str, Any] = {
            "process_run_id": self.process_run_id,
            "task_id": task_id,
            "process_type": task_type,
        }
        if skip_semantic:
            memory_payload["skip_vector"] = True
        else:
            memory_payload["semantic_query"] = user_intent[:300]
        memory_block = (initial_memory_block or "").strip()
        if not memory_block:
            memory_block = await workflow.execute_activity(
                build_process_memory_context,
                memory_payload,
                start_to_close_timeout=timedelta(seconds=120),
            )

        for plan_step in steps:
            step_name = plan_step.get("name", "execute")
            predicate_id = plan_step.get("predicate_id", "generic_deliver")
            step_prompt = plan_step.get("prompt", "")
            for attempt in range(1, 4):
                result = await workflow.execute_child_workflow(
                    GenericExecuteChildWorkflow.run,
                    {
                        "task_id": task_id,
                        "session_key": session_key,
                        "user_intent": user_intent,
                        "process_run_id": self.process_run_id,
                        "step_name": step_name,
                        "step_prompt": step_prompt,
                        "predicate_id": predicate_id,
                        "attempt": attempt,
                        "max_attempts": 3,
                        "memory_block": memory_block,
                        "context_block": step_context,
                        "generic_profile": generic_profile,
                        "user_time_block": user_time_block,
                        # Conversational: return agent text ASAP; write episodic after Slack.
                        "defer_episodic_write": is_conversational,
                    },
                    id=f"{task_id}-plan-{step_name}-{attempt}",
                    task_queue=workflow.info().task_queue,
                )
                status = result.get("status", "pending")
                text = result.get("text", "")
                if status == "completed":
                    step_context += f"\n[{step_name}]: {text[:500]}"
                    final_text = text
                    if not is_conversational:
                        memory_block = await workflow.execute_activity(
                            build_process_memory_context,
                            memory_payload,
                            start_to_close_timeout=timedelta(seconds=120),
                        )
                    break
                if status in ("failed", "blocked"):
                    if attempt >= 3 and step_context.strip():
                        await workflow.execute_activity(
                            execute_compensation,
                            {
                                "task_id": task_id,
                                "process_run_id": self.process_run_id,
                                "reason": result.get("reason", status),
                            },
                            start_to_close_timeout=timedelta(seconds=15),
                        )
                        return {
                            "status": "compensated",
                            "task_id": task_id,
                            "reason": result.get("reason"),
                        }
                    if status == "blocked":
                        await workflow.execute_activity(
                            update_task_status,
                            {
                                "task_id": task_id,
                                "status": "pending_user_input",
                                "next_check_minutes": 60,
                            },
                            start_to_close_timeout=timedelta(seconds=10),
                        )
                        await workflow.wait_condition(lambda: len(self.user_inputs) > 0)
                        continue
                    break

        from app.notification_policy import sanitize_user_facing_text
        from app.orchestrator.step_predicates import extract_agent_facts

        raw_result = final_text or step_context
        extracted = extract_agent_facts(raw_result)
        clean_result = sanitize_user_facing_text(
            extracted.get("body") or raw_result
        )

        if is_conversational and clean_result.strip():
            # Slack first (perceived latency); status/memory afterward (fail-soft).
            if not is_internal_task(user_intent, task_type, tags):
                await workflow.execute_activity(
                    notify_slack_user,
                    {
                        "session_key": session_key,
                        "task_id": task_id,
                        "intent": user_intent,
                        "task_type": task_type,
                        "tags": tags,
                        "message": clean_result[:3000],
                    },
                    start_to_close_timeout=timedelta(seconds=30),
                )
            await workflow.execute_activity(
                update_process_state,
                {
                    "process_run_id": self.process_run_id,
                    "state": "completed",
                    "ended": True,
                },
                start_to_close_timeout=timedelta(seconds=10),
            )
            await workflow.execute_activity(
                update_task_status,
                {"task_id": task_id, "status": "completed"},
                start_to_close_timeout=timedelta(seconds=10),
            )
            await workflow.execute_activity(
                write_episodic_observation,
                {
                    "process_run_id": self.process_run_id,
                    "task_id": task_id,
                    "text": clean_result[:4000],
                },
                start_to_close_timeout=timedelta(seconds=90),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            await workflow.execute_activity(
                promote_completion_memory,
                {
                    "process_run_id": self.process_run_id,
                    "process_type": task_type,
                    "task_id": task_id,
                    "content": clean_result[:3000],
                },
                start_to_close_timeout=timedelta(seconds=30),
            )
            return {"status": "completed", "task_id": task_id, "final_result": clean_result}

        if task_type == "canary":
            max_rework = 0
        else:
            max_rework = rework_max_attempts
        for rework_attempt in range(1, max_rework + 1):
            evidence = check_evidence(user_intent, clean_result)
            skip_quality = evidence_high_confidence(user_intent, clean_result)
            quality = {"quality": "pass"}
            if not skip_quality:
                quality = await workflow.execute_activity(
                    verify_response_quality,
                    {
                        "task_id": task_id,
                        "user_intent": user_intent,
                        "agent_response": clean_result[:2000],
                    },
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            gate = decide_completion_gate(
                evidence_passed=evidence["passed"],
                evidence_issues=evidence.get("issues", []),
                quality_passed=quality.get("quality") != "fail",
                quality_issues=quality.get("issues", ""),
                skip_quality_llm=skip_quality,
            )
            if gate["action"] == "complete":
                break
            if rework_attempt >= max_rework:
                await workflow.execute_activity(
                    finalize_task_failure,
                    {
                        "task_id": task_id,
                        "process_run_id": self.process_run_id,
                        "task_status": "failed",
                        "process_state": "failed_terminal",
                    },
                    start_to_close_timeout=timedelta(seconds=10),
                )
                if not is_internal_task(user_intent, task_type, tags):
                    await workflow.execute_activity(
                        notify_slack_user,
                        {
                            "session_key": session_key,
                            "task_id": task_id,
                            "message": f"Could not complete: {gate.get('reason', 'quality/evidence failed')}",
                        },
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                return {"status": "failed", "task_id": task_id}
            rework_prompt = build_rework_prompt(
                user_intent,
                clean_result,
                evidence_issues=evidence.get("issues"),
                quality_issues=quality.get("issues", ""),
                attempt=rework_attempt,
                max_attempts=max_rework,
            )
            rework_resp = await workflow.execute_activity(
                send_to_openclaw,
                {
                    "message": rework_prompt,
                    "task_id": task_id,
                    "session_key": session_key,
                },
                start_to_close_timeout=timedelta(minutes=45),
            )
            try:
                clean_result = rework_resp["result"]["payloads"][0]["text"]
            except Exception:
                clean_result = str(rework_resp)
            from app.orchestrator.completion_rework import should_admit_failure

            if should_admit_failure(rework_attempt, max_rework, clean_result):
                await workflow.execute_activity(
                    finalize_task_failure,
                    {
                        "task_id": task_id,
                        "process_run_id": self.process_run_id,
                        "task_status": "failed",
                        "process_state": "failed_terminal",
                    },
                    start_to_close_timeout=timedelta(seconds=10),
                )
                if not is_internal_task(user_intent, task_type, tags):
                    await workflow.execute_activity(
                        notify_slack_user,
                        {
                            "session_key": session_key,
                            "task_id": task_id,
                            "message": clean_result[:1500],
                        },
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                return {"status": "failed", "task_id": task_id}
            extracted = extract_agent_facts(clean_result)
            clean_result = sanitize_user_facing_text(
                extracted.get("body") or clean_result
            )

        if not is_internal_task(user_intent, task_type, tags):
            await workflow.execute_activity(
                notify_slack_user,
                {
                    "session_key": session_key,
                    "task_id": task_id,
                    "intent": user_intent,
                    "task_type": task_type,
                    "tags": tags,
                    "message": clean_result[:3000],
                },
                start_to_close_timeout=timedelta(seconds=30),
            )
        await workflow.execute_activity(
            update_process_state,
            {
                "process_run_id": self.process_run_id,
                "state": "completed",
                "ended": True,
            },
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity(
            update_task_status,
            {"task_id": task_id, "status": "completed"},
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity(
            promote_completion_memory,
            {
                "process_run_id": self.process_run_id,
                "process_type": task_type,
                "task_id": task_id,
                "content": clean_result[:3000],
            },
            start_to_close_timeout=timedelta(seconds=30),
        )
        return {"status": "completed", "task_id": task_id, "final_result": clean_result}

