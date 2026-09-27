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
    )
    from app.evidence import check_evidence
    from app.orchestrator.process_brief import (
        compose_executor_memory,
        ensure_brief_header,
        format_user_catchup,
    )
    from app.orchestrator.decision_engine import decide_completion_gate
    from app.orchestrator.completion_rework import (
        build_escalation_message,
        build_rework_prompt,
        build_strategy_change_prompt,
        next_loop_action,
    )
    from app.workflows.generic_execute_child import GenericExecuteChildWorkflow
    from app.task_registry.stop_command import is_whole_message_stop
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
        self._catchup_chunks: List[str] = []

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

    def _stop_pending(self) -> bool:
        if self._cancel_requested:
            return True
        return any(is_whole_message_stop(str(msg)) for msg in self.user_inputs)

    async def _finish_stop(
        self,
        task_id: str,
        session_key: str,
        user_intent: str,
        task_type: str,
        tags: List[str],
    ) -> Dict[str, Any]:
        self.user_inputs = [
            msg for msg in self.user_inputs if not is_whole_message_stop(str(msg))
        ]
        await workflow.execute_activity(
            update_task_status,
            {"task_id": task_id, "status": "stopped_by_user"},
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity(
            update_process_state,
            {
                "process_run_id": self.process_run_id,
                "state": "stopped_by_user",
                "ended": True,
            },
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity(
            notify_slack_user,
            {
                "session_key": session_key,
                "task_id": task_id,
                "intent": user_intent,
                "task_type": task_type,
                "tags": tags,
                "message": f"Task {task_id[:8]} stopped as requested.",
            },
            start_to_close_timeout=timedelta(seconds=30),
        )
        return {"status": "stopped_by_user", "task_id": task_id}

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
                rework_max_attempts=int(payload.get("rework_max_attempts") or 20),
                user_time_block=user_time_block,
                execution_mode=execution_mode,
                attempt_policy={
                    "max_attempts": int(payload.get("rework_max_attempts") or 20),
                    "strategy_change_attempt": int(payload.get("strategy_change_attempt") or 10),
                    "escalate_user_attempt": int(payload.get("escalate_user_attempt") or 20),
                },
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

        if isinstance(result, dict) and result.get("status") == "stopped_by_user":
            return result

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
                    rework_max_attempts=int(payload.get("rework_max_attempts") or 20),
                    user_time_block=user_time_block,
                    execution_mode=execution_mode,
                    attempt_policy={
                        "max_attempts": int(payload.get("rework_max_attempts") or 20),
                        "strategy_change_attempt": int(payload.get("strategy_change_attempt") or 10),
                        "escalate_user_attempt": int(payload.get("escalate_user_attempt") or 20),
                    },
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
        rework_max_attempts: int = 20,
        user_time_block: str = "",
        execution_mode: str = "",
        attempt_policy: Dict[str, int] | None = None,
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
        memory_fetched = await workflow.execute_activity(
            build_process_memory_context,
            memory_payload,
            start_to_close_timeout=timedelta(seconds=120),
        )
        memory_block = compose_executor_memory(
            ensure_brief_header(initial_memory_block or ""),
            memory_fetched,
        )

        for plan_step in steps:
            step_name = plan_step.get("name", "execute")
            predicate_id = plan_step.get("predicate_id", "generic_deliver")
            step_prompt = plan_step.get("prompt", "")
            if self._stop_pending():
                return await self._finish_stop(
                    task_id, session_key, user_intent, task_type, tags
                )
            while self.user_inputs:
                reply = self.user_inputs.pop(0).strip()
                if reply:
                    self._catchup_chunks.append(reply)
            if self._catchup_chunks:
                step_context += "\n" + format_user_catchup(self._catchup_chunks)
                self._catchup_chunks.clear()
            for attempt in range(1, 4):
                if self._stop_pending():
                    return await self._finish_stop(
                        task_id, session_key, user_intent, task_type, tags
                    )
                handle = await workflow.start_child_workflow(
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
                        "task_type": task_type,
                        "tags": tags,
                        # Conversational: return agent text ASAP; write episodic after Slack.
                        "defer_episodic_write": is_conversational,
                    },
                    id=f"{task_id}-plan-{step_name}-{attempt}",
                    task_queue=workflow.info().task_queue,
                    parent_close_policy=workflow.ParentClosePolicy.TERMINATE,
                )
                await workflow.wait_condition(
                    lambda: handle.done() or self._stop_pending()
                )
                if self._stop_pending():
                    return await self._finish_stop(
                        task_id, session_key, user_intent, task_type, tags
                    )
                result = await handle
                status = result.get("status", "pending")
                text = result.get("text", "")
                if status == "completed":
                    step_context += f"\n[{step_name}]: {text[:500]}"
                    final_text = text
                    if not is_conversational:
                        memory_fetched = await workflow.execute_activity(
                            build_process_memory_context,
                            memory_payload,
                            start_to_close_timeout=timedelta(seconds=120),
                        )
                        memory_block = compose_executor_memory(
                            ensure_brief_header(initial_memory_block or ""),
                            memory_fetched,
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
                        if self._stop_pending():
                            return await self._finish_stop(
                                task_id, session_key, user_intent, task_type, tags
                            )
                        if self.user_inputs:
                            reply = self.user_inputs.pop(0).strip()
                            if reply:
                                step_context += "\n" + format_user_catchup([reply])
                        continue
                    break

        from app.notification_policy import sanitize_user_facing_text
        from app.orchestrator.step_predicates import extract_agent_facts

        raw_result = final_text or step_context
        extracted = extract_agent_facts(raw_result)
        clean_result = sanitize_user_facing_text(
            extracted.get("body") or raw_result
        )

        policy = attempt_policy or {
            "max_attempts": int(rework_max_attempts or 20),
            "strategy_change_attempt": 10,
            "escalate_user_attempt": 20,
        }
        if task_type == "canary" or "canary" in {t.lower() for t in tags}:
            max_rework = 0
        else:
            max_rework = int(policy["max_attempts"])
        for rework_attempt in range(1, max_rework + 1):
            evidence = check_evidence(user_intent, clean_result)
            skip_quality = is_internal_task(user_intent, task_type, tags)
            quality = {"quality": "pass", "verdict": "accept"}
            if not skip_quality:
                quality = await workflow.execute_activity(
                    verify_response_quality,
                    {
                        "task_id": task_id,
                        "user_intent": user_intent,
                        "agent_response": clean_result[:4000],
                        "process_run_id": self.process_run_id,
                        "attempt": rework_attempt,
                        "process_brief": initial_memory_block or "",
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
            action = next_loop_action(rework_attempt, policy)
            if action == "escalate_user":
                diag = build_escalation_message(
                    user_intent,
                    clean_result,
                    evidence_issues=evidence.get("issues"),
                    quality_issues=quality.get("issues", ""),
                    attempts=rework_attempt,
                )
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
                            "intent": user_intent,
                            "task_type": task_type,
                            "tags": tags,
                            "message": diag[:3000],
                        },
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                await workflow.execute_activity(
                    record_event,
                    {
                        "correlation_id": task_id,
                        "entity_type": "task",
                        "entity_id": task_id,
                        "event_type": "evaluator.escalate",
                        "event_payload": {"attempt": rework_attempt},
                    },
                    start_to_close_timeout=timedelta(seconds=10),
                )
                return {"status": "failed", "task_id": task_id, "reason": "escalated"}
            prompt_fn = (
                build_strategy_change_prompt
                if action == "strategy_change"
                else build_rework_prompt
            )
            rework_prompt = prompt_fn(
                user_intent,
                clean_result,
                evidence_issues=evidence.get("issues"),
                quality_issues=quality.get("issues", ""),
                command_to_aura=quality.get("command_to_aura", ""),
                attempt=rework_attempt + 1,
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

