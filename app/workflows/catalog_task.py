"""Step-driven workflow using catalog templates."""
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from app.activities.openclaw_activities import (
        check_intermediate_updates_enabled,
        notify_slack_user,
        parse_agent_evaluation,
        send_to_openclaw,
        validate_openclaw_output,
    )
    from app.task_registry.stop_command import is_whole_message_stop
    from app.activities.db_activities import (
        acquire_process_run_lease,
        build_process_memory_context,
        ensure_process_run,
        finalize_task_failure,
        list_process_artifacts,
        promote_completion_memory,
        read_process_memory,
        execute_compensation,
        record_event,
        record_observation,
        record_step,
        register_artifact,
        update_process_state,
        update_task_status,
        write_episodic_observation,
        write_process_memory,
    )
    from app.evidence import (
        check_catalog_completion,
        check_completion_artifact,
        check_evidence,
    )
    from app.orchestrator.completion_rework import (
        build_escalation_message,
        build_rework_prompt,
        build_strategy_change_prompt,
        next_loop_action,
    )
    from app.orchestrator.decision_engine import decide_completion_gate
    from app.workflows.catalog import get_template
    from app.notification_policy import format_workflow_error, is_silent_system_ack
    from app.workflows.catalog_step_child import CatalogStepChildWorkflow
    from app.workflows.generic_task import is_heartbeat_ack, is_heartbeat_request, strip_json_eval
    from app.orchestrator.process_brief import (
        compose_executor_memory,
        ensure_brief_header,
        format_user_catchup,
    )
    from app.workflows.judgment import EvaluatorRetry
    from app.workflows.user_messages import AttachedMessages


@workflow.defn
class CatalogTaskWorkflow(AttachedMessages, EvaluatorRetry):
    def __init__(self) -> None:
        self.user_inputs: List[str] = []
        self.process_run_id: str = ""
        self._cancel_requested: bool = False
        self._retry_requested: bool = False
        self._approved: bool = False
        self._spawn_leg_requested: bool = False
        self._spawn_leg_payload: Dict[str, Any] = {}
        self._catchup_chunks: List[str] = []
        self._initial_memory_block: str = ""

    @workflow.signal
    def spawn_leg(self, payload: Dict[str, Any]) -> None:
        self._spawn_leg_payload = payload or {}
        self._spawn_leg_requested = True

    @workflow.signal
    def user_input(self, message: str) -> None:
        self.user_inputs.append(message)

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

    async def _stop_task(
        self, task_id: str, session_key: str, stop_message: str
    ) -> Dict[str, Any]:
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
            {"session_key": session_key, "task_id": task_id, "message": stop_message},
            start_to_close_timeout=timedelta(seconds=30),
        )
        return {"status": "stopped_by_user", "task_id": task_id}

    async def _consume_stop(self, task_id: str, session_key: str) -> Optional[Dict[str, Any]]:
        if self._cancel_requested:
            return await self._stop_task(
                task_id, session_key, f"Task {task_id[:8]} cancelled via signal."
            )
        while self.user_inputs:
            reply = self.user_inputs.pop(0).strip()
            if is_whole_message_stop(reply):
                return await self._stop_task(
                    task_id, session_key, f"Task {task_id[:8]} stopped as requested."
                )
            if reply:
                self._catchup_chunks.append(reply)
        return None

    async def _finish_durable_catalog(
        self,
        payload: Dict[str, Any],
        user_intent: str,
        result: Dict[str, Any],
        task_kind: str,
    ) -> Dict[str, Any]:
        """After a completed catalog leg, wait for spawn_leg and continue-as-new."""
        if task_kind != "durable" or result.get("status") != "completed":
            return result
        while not self._cancel_requested:
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
            new_payload = {
                **payload,
                "intent": leg_intent,
                "process_run_id": self.process_run_id,
                "_durable_leg": True,
            }
            await self._resubmit_leftovers(
                payload.get("task_id", "unknown"), payload.get("session_key", "agent:main:main")
            )
            workflow.continue_as_new(new_payload)
        return result

    async def _judge_or_close(
        self, verify_payload: Dict[str, Any], *, session_key: str, user_intent: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """The verdict, or the task's closing result when a stop arrived or the reviewer stayed down."""
        task_id = verify_payload["task_id"]
        task_type, tags = payload.get("task_type", ""), payload.get("tags") or []
        quality = await self._judge(
            verify_payload, session_key=session_key, user_intent=user_intent, task_type=task_type, tags=tags
        )
        if quality is not None:
            return quality
        stopped = await self._consume_stop(task_id, session_key)
        if stopped:
            return stopped
        return await self._reviewer_unavailable(task_id, session_key, user_intent, task_type, tags)

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._run(payload)
        await self._resubmit_leftovers(
            payload.get("task_id", "unknown"), payload.get("session_key", "agent:main:main")
        )
        return result

    async def _run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        task_id = payload.get("task_id", "unknown")
        user_intent = payload.get("intent", "")
        session_key = payload.get("session_key", "agent:main:main")
        correlation_id = payload.get("correlation_id", task_id)
        rework_max_attempts = int(payload.get("rework_max_attempts") or 20)
        attempt_policy = {
            "max_attempts": rework_max_attempts,
            "strategy_change_attempt": int(payload.get("strategy_change_attempt") or 10),
            "escalate_user_attempt": int(payload.get("escalate_user_attempt") or 20),
        }
        process_type_raw = payload.get("process_type") or payload.get("task_type", "generic")
        self._initial_memory_block = (payload.get("initial_memory_block") or "").strip()
        # Trust the process_type already assigned at intake/start — do not
        # re-match intent keywords (that would reintroduce hard routing).
        from app.workflows.catalog import get_template, normalize_catalog_type

        process_type = normalize_catalog_type(str(process_type_raw), "") or str(process_type_raw)
        if not get_template(process_type):
            raise ValueError(
                f"Unknown catalog process_type: {process_type_raw} "
                f"(resolved={process_type!r}); not in workflow catalog"
            )
        template = get_template(process_type)

        task_kind = payload.get("task_kind", "one_shot")
        durable_leg = bool(payload.get("_durable_leg"))

        if durable_leg and payload.get("process_run_id"):
            self.process_run_id = str(payload["process_run_id"])
        else:
            self.process_run_id = await workflow.execute_activity(
                ensure_process_run,
                {
                    "task_id": task_id,
                    "process_type": template.process_type,
                    "success_criteria": template.success_criteria,
                },
                start_to_close_timeout=timedelta(seconds=15),
            )

            await workflow.execute_activity(
                record_event,
                {
                    "correlation_id": correlation_id,
                    "entity_type": "task",
                    "entity_id": task_id,
                    "event_type": "task.started",
                    "event_payload": {
                        "intent": user_intent[:500],
                        "process_type": template.process_type,
                        "template_version": template.version,
                        "catalog": True,
                    },
                },
                start_to_close_timeout=timedelta(seconds=10),
            )

            await workflow.execute_activity(
                update_task_status,
                {"task_id": task_id, "status": "running", "next_check_minutes": 10},
                start_to_close_timeout=timedelta(seconds=10),
            )

            await workflow.execute_activity(
                write_process_memory,
                {
                    "scope_type": "process",
                    "scope_id": self.process_run_id,
                    "memory_type": "working",
                    "content": f"Catalog workflow: {template.display_name}",
                    "provenance_ref": {"process_type": template.process_type},
                },
                start_to_close_timeout=timedelta(seconds=10),
            )
            if self._initial_memory_block:
                await workflow.execute_activity(
                    write_process_memory,
                    {
                        "scope_type": "process",
                        "scope_id": self.process_run_id,
                        "memory_type": "working",
                        "content": self._initial_memory_block[:8000],
                        "provenance_ref": {"kind": "process_brief"},
                    },
                    start_to_close_timeout=timedelta(seconds=10),
                )
        if durable_leg:
            await workflow.execute_activity(
                update_task_status,
                {"task_id": task_id, "status": "running", "next_check_minutes": 10},
                start_to_close_timeout=timedelta(seconds=10),
            )

        step_context = ""
        accumulated_output = ""

        try:
            total_steps = len(template.steps)
            for step_idx, step in enumerate(template.steps, start=1):
                stop = await self._consume_stop(task_id, session_key)
                if stop:
                    return stop

                if step.kind == "approval_gate":
                    await workflow.execute_activity(
                        record_step,
                        {
                            "process_run_id": self.process_run_id,
                            "step_name": step.name,
                            "step_kind": step.kind,
                            "attempt_no": 1,
                            "idempotency_key": f"{task_id}-{step.name}-1",
                            "input_ref": {"step": step.name},
                        },
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    await workflow.execute_activity(
                        update_process_state,
                        {
                            "process_run_id": self.process_run_id,
                            "state": "awaiting_approval",
                        },
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    await workflow.execute_activity(
                        update_task_status,
                        {
                            "task_id": task_id,
                            "status": "pending_user_input",
                            "next_check_minutes": 60,
                        },
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    send_updates = await workflow.execute_activity(
                        check_intermediate_updates_enabled,
                        {},
                        start_to_close_timeout=timedelta(seconds=5),
                    )
                    approval_msg = step.user_update or (
                        f"Approval required for step '{step.name}'. "
                        "Reply 'approve' to continue or 'stop' to cancel."
                    )
                    # Approval gates always notify — they block progress. Do not
                    # treat them as optional intermediate updates.
                    await workflow.execute_activity(
                        notify_slack_user,
                        {"session_key": session_key, "task_id": task_id, "message": approval_msg},
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                    if not send_updates:
                        workflow.logger.info(
                            "approval_gate notified despite intermediate_updates=off task=%s",
                            task_id,
                        )
                    approved = False
                    while not approved:
                        await workflow.wait_condition(
                            lambda: len(self.user_inputs) > 0 or self._approved
                        )
                        if self._approved:
                            self._approved = False
                            approved = True
                            break
                        reply = self.user_inputs.pop(0).strip()
                        if re.search(
                            r"\b(stop|abort|cancel|halt|reject|deny)\b", reply.lower()
                        ):
                            return await self._stop_task(
                                task_id,
                                session_key,
                                f"Task {task_id[:8]} stopped at approval gate.",
                            )
                        if re.search(
                            r"\b(approve|approved|yes|go\s+ahead|proceed|ok)\b",
                            reply.lower(),
                        ):
                            approved = True
                            break
                        await workflow.execute_activity(
                            notify_slack_user,
                            {
                                "session_key": session_key,
                                "task_id": task_id,
                                "message": (
                                    "Approval not recognized. Reply 'approve' to continue "
                                    "or 'stop' to cancel."
                                ),
                            },
                            start_to_close_timeout=timedelta(seconds=30),
                        )
                    await workflow.execute_activity(
                        update_process_state,
                        {
                            "process_run_id": self.process_run_id,
                            "state": "running",
                        },
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    await workflow.execute_activity(
                        update_task_status,
                        {"task_id": task_id, "status": "running", "next_check_minutes": 10},
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    step_context += f"\nApproval gate '{step.name}': approved"
                    continue

                if step.kind == "wait_external":
                    await workflow.execute_activity(
                        update_process_state,
                        {
                            "process_run_id": self.process_run_id,
                            "state": "waiting_external",
                            "next_check_minutes": step.wait_minutes,
                        },
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    await workflow.execute_activity(
                        update_task_status,
                        {
                            "task_id": task_id,
                            "status": "pending_user_input",
                            "next_check_minutes": step.wait_minutes,
                        },
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    send_updates = await workflow.execute_activity(
                        check_intermediate_updates_enabled,
                        {},
                        start_to_close_timeout=timedelta(seconds=5),
                    )
                    if send_updates and step.user_update:
                        await workflow.execute_activity(
                            notify_slack_user,
                            {"session_key": session_key, "task_id": task_id, "message": step.user_update},
                            start_to_close_timeout=timedelta(seconds=30),
                        )
                    await workflow.sleep(timedelta(minutes=step.wait_minutes))
                    await workflow.execute_activity(
                        update_task_status,
                        {"task_id": task_id, "status": "running", "next_check_minutes": 10},
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    await workflow.execute_activity(
                        update_process_state,
                        {
                            "process_run_id": self.process_run_id,
                            "state": "running",
                        },
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    continue

                step_done = False
                step_feedback = ""
                for attempt in range(1, step.max_attempts + 1):
                    stop = await self._consume_stop(task_id, session_key)
                    if stop:
                        return stop

                    memory_fetched = await workflow.execute_activity(
                        build_process_memory_context,
                        {
                            "process_run_id": self.process_run_id,
                            "semantic_query": user_intent[:300],
                            "task_id": task_id,
                            "process_type": template.process_type,
                        },
                        start_to_close_timeout=timedelta(seconds=15),
                    )
                    memory_block = compose_executor_memory(
                        ensure_brief_header(self._initial_memory_block),
                        memory_fetched,
                    )

                    if self._catchup_chunks:
                        step_context += "\n" + format_user_catchup(self._catchup_chunks)
                        self._catchup_chunks.clear()

                    context_block = step_context
                    if step_feedback:
                        context_block += f"\nPREVIOUS ATTEMPT FEEDBACK:\n{step_feedback}\n"

                    send_updates = await workflow.execute_activity(
                        check_intermediate_updates_enabled,
                        {},
                        start_to_close_timeout=timedelta(seconds=5),
                    )
                    progress_msg = (
                        f"Step {step_idx}/{total_steps} `{step.name}`"
                        f" — attempt {attempt}/{step.max_attempts}"
                    )
                    if step.user_update:
                        progress_msg += f": {step.user_update}"
                    if send_updates:
                        await workflow.execute_activity(
                            notify_slack_user,
                            {
                                "session_key": session_key,
                                "task_id": task_id,
                                "message": progress_msg,
                            },
                            start_to_close_timeout=timedelta(seconds=30),
                        )

                    result = await workflow.execute_child_workflow(
                        CatalogStepChildWorkflow.run,
                        {
                            "task_id": task_id,
                            "session_key": session_key,
                            "user_intent": user_intent,
                            "process_run_id": self.process_run_id,
                            "process_type": template.process_type,
                            "step_name": step.name,
                            "step_prompt": step.prompt,
                            "predicate_id": step.predicate_id,
                            "attempt": attempt,
                            "max_attempts": step.max_attempts,
                            "context_block": context_block,
                            "memory_block": memory_block,
                            "task_id_for_memory": task_id,
                        },
                        id=f"{task_id}-{step.name}-{attempt}",
                        task_queue=workflow.info().task_queue,
                    )
                    status = result.get("status", "pending")
                    reason = result.get("reason", "")
                    text = result.get("text", "")

                    if status == "failed":
                        if step_context.strip():
                            await workflow.execute_activity(
                                execute_compensation,
                                {
                                    "task_id": task_id,
                                    "process_run_id": self.process_run_id,
                                    "reason": f"Failed at step '{step.name}': {reason}",
                                },
                                start_to_close_timeout=timedelta(seconds=10),
                            )
                            await workflow.execute_activity(
                                notify_slack_user,
                                {
                                    "session_key": session_key,
                                    "task_id": task_id,
                                    "message": (
                                        f"Task compensated at step '{step.name}': {reason}"
                                    ),
                                },
                                start_to_close_timeout=timedelta(seconds=30),
                            )
                            return {
                                "status": "compensated",
                                "task_id": task_id,
                                "step": step.name,
                            }
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
                        await workflow.execute_activity(
                            notify_slack_user,
                            {
                                "session_key": session_key,
                                "task_id": task_id,
                                "message": f"Task failed at step '{step.name}': {reason}",
                            },
                            start_to_close_timeout=timedelta(seconds=30),
                        )
                        return {"status": "failed", "task_id": task_id, "step": step.name}

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
                        await workflow.execute_activity(
                            update_process_state,
                            {
                                "process_run_id": self.process_run_id,
                                "state": "blocked",
                            },
                            start_to_close_timeout=timedelta(seconds=10),
                        )
                        await workflow.execute_activity(
                            notify_slack_user,
                            {
                                "session_key": session_key,
                                "task_id": task_id,
                                "message": (
                                    f"Blocked at step '{step.name}': {reason}. "
                                    "Reply with guidance or 'stop' to cancel."
                                ),
                            },
                            start_to_close_timeout=timedelta(seconds=30),
                        )
                        await workflow.wait_condition(lambda: len(self.user_inputs) > 0)
                        reply = self.user_inputs.pop(0)
                        if is_whole_message_stop(reply):
                            return await self._stop_task(
                                task_id, session_key, f"Task {task_id[:8]} stopped."
                            )
                        step_feedback = f"User guidance: {reply}"
                        continue

                    if status == "completed":
                        clean = strip_json_eval(text)
                        accumulated_output += f"\n[{step.name}] {clean[:800]}"
                        await workflow.execute_activity(
                            write_process_memory,
                            {
                                "scope_type": "process",
                                "scope_id": self.process_run_id,
                                "memory_type": "working",
                                "content": f"{step.name}: {clean[:500]}",
                                "provenance_ref": {"step": step.name},
                            },
                            start_to_close_timeout=timedelta(seconds=10),
                        )
                        step_context += f"\nCompleted step '{step.name}': {reason or 'ok'}"
                        step_done = True
                        break

                    step_feedback = reason or "Step incomplete; retry."

                if not step_done:
                    if step_context.strip():
                        await workflow.execute_activity(
                            execute_compensation,
                            {
                                "task_id": task_id,
                                "process_run_id": self.process_run_id,
                                "reason": f"Step '{step.name}' failed after {step.max_attempts} attempts.",
                            },
                            start_to_close_timeout=timedelta(seconds=15),
                        )
                        await workflow.execute_activity(
                            notify_slack_user,
                            {
                                "session_key": session_key,
                                "task_id": task_id,
                                "message": f"Task compensated: step '{step.name}' exhausted retries.",
                            },
                            start_to_close_timeout=timedelta(seconds=30),
                        )
                        return {
                            "status": "compensated",
                            "task_id": task_id,
                            "step": step.name,
                        }
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
                    await workflow.execute_activity(
                        notify_slack_user,
                        {
                            "session_key": session_key,
                            "task_id": task_id,
                            "message": f"Step '{step.name}' failed after {step.max_attempts} attempts.",
                        },
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                    return {"status": "failed", "task_id": task_id, "step": step.name}

            clean_result = accumulated_output.strip() or "Workflow steps completed."
            added = self._take_user_messages()
            if added:
                clean_result = await self._fold_in(
                    task_id, session_key, payload.get("task_type", ""),
                    payload.get("tags") or [], clean_result, added,
                )
            catalog_evidence = check_catalog_completion(
                template.process_type, user_intent, clean_result
            )
            base_evidence = check_evidence(user_intent, clean_result)
            evidence_issues = list(catalog_evidence["issues"]) + list(base_evidence["issues"])
            evidence_ok = catalog_evidence["passed"] and base_evidence["passed"]

            artifact = await workflow.execute_activity(
                register_artifact,
                {
                    "process_run_id": self.process_run_id,
                    "kind": "completion_output",
                    "content": clean_result[:50000],
                    "filename": f"{template.process_type}-{task_id[:8]}.txt",
                    "mime_type": "text/plain; charset=utf-8",
                },
                start_to_close_timeout=timedelta(seconds=30),
            )
            required_kinds = template.success_criteria.get(
                "required_artifact_kinds", ["completion_output"]
            )
            if not artifact.get("skipped"):
                all_artifacts = await workflow.execute_activity(
                    list_process_artifacts,
                    {"process_run_id": self.process_run_id},
                    start_to_close_timeout=timedelta(seconds=15),
                )
                artifact_evidence = check_completion_artifact(
                    all_artifacts, required_kinds=required_kinds
                )
                if not artifact_evidence["passed"]:
                    evidence_ok = False
                    evidence_issues = evidence_issues + list(artifact_evidence["issues"])

            quality = await self._judge_or_close(
                {
                    "task_id": task_id,
                    "user_intent": user_intent,
                    "agent_response": clean_result,
                    "process_run_id": self.process_run_id,
                    "attempt": 1,
                    "process_brief": self._initial_memory_block or "",
                },
                session_key=session_key,
                user_intent=user_intent,
                payload=payload,
            )
            if quality.get("status") in ("failed", "stopped_by_user"):
                return quality
            gate = decide_completion_gate(
                evidence_passed=evidence_ok,
                evidence_issues=evidence_issues,
                quality_passed=quality.get("quality") != "fail",
                quality_issues=quality.get("issues", ""),
                skip_quality_llm=False,
            )
            if gate["action"] == "retry" or quality.get("quality") == "fail":
                policy = attempt_policy
                max_rework = rework_max_attempts or int(policy["max_attempts"])
                rework_ok = False
                judged = 1
                while True:
                    action = next_loop_action(judged, policy)
                    if action == "escalate_user":
                        rework_ok = False
                        break
                    prompt_fn = (
                        build_strategy_change_prompt
                        if action == "strategy_change"
                        else build_rework_prompt
                    )
                    rework_prompt = prompt_fn(
                        user_intent,
                        clean_result,
                        evidence_issues=evidence_issues,
                        quality_issues=quality.get("issues", ""),
                        command_to_aura=quality.get("command_to_aura", ""),
                        attempt=judged + 1,
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
                    from app.notification_policy import sanitize_user_facing_text
                    from app.orchestrator.step_predicates import extract_agent_facts

                    clean_result = sanitize_user_facing_text(
                        extract_agent_facts(clean_result).get("body") or clean_result
                    )
                    added = self._take_user_messages()
                    if added:
                        clean_result = await self._fold_in(
                            task_id, session_key, payload.get("task_type", ""),
                            payload.get("tags") or [], clean_result, added,
                        )
                    judged += 1
                    catalog_evidence = check_catalog_completion(
                        template.process_type, user_intent, clean_result
                    )
                    base_evidence = check_evidence(user_intent, clean_result)
                    evidence_issues = list(catalog_evidence["issues"]) + list(
                        base_evidence["issues"]
                    )
                    quality = await self._judge_or_close(
                        {
                            "task_id": task_id,
                            "user_intent": user_intent,
                            "agent_response": clean_result,
                            "process_run_id": self.process_run_id,
                            "attempt": judged,
                            "process_brief": self._initial_memory_block or "",
                        },
                        session_key=session_key,
                        user_intent=user_intent,
                        payload=payload,
                    )
                    if quality.get("status") in ("failed", "stopped_by_user"):
                        return quality
                    gate = decide_completion_gate(
                        evidence_passed=catalog_evidence["passed"]
                        and base_evidence["passed"],
                        evidence_issues=evidence_issues,
                        quality_passed=quality.get("quality") != "fail",
                        quality_issues=quality.get("issues", ""),
                    )
                    if gate["action"] == "complete":
                        rework_ok = True
                        break
                if not rework_ok:
                    diag = build_escalation_message(
                        user_intent,
                        clean_result,
                        evidence_issues=evidence_issues,
                        quality_issues=quality.get("issues", ""),
                        attempts=judged,
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
                    await workflow.execute_activity(
                        notify_slack_user,
                        {
                            "session_key": session_key,
                            "task_id": task_id,
                            "intent": user_intent,
                            "task_type": payload.get("task_type", ""),
                            "tags": payload.get("tags") or [],
                            "message": diag[:3000],
                        },
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                    return {"status": "failed", "task_id": task_id}

            await workflow.execute_activity(
                write_process_memory,
                {
                    "scope_type": "process",
                    "scope_id": self.process_run_id,
                    "memory_type": "episodic",
                    "content": f"Completed {template.display_name}: {clean_result[:1000]}",
                    "provenance_ref": {"task_id": task_id},
                },
                start_to_close_timeout=timedelta(seconds=10),
            )
            await workflow.execute_activity(
                promote_completion_memory,
                {
                    "process_run_id": self.process_run_id,
                    "process_type": template.process_type,
                    "task_id": task_id,
                    "content": clean_result[:3000],
                },
                start_to_close_timeout=timedelta(seconds=30),
            )
            await workflow.execute_activity(
                update_task_status,
                {"task_id": task_id, "status": "completed"},
                start_to_close_timeout=timedelta(seconds=10),
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
                record_event,
                {
                    "correlation_id": correlation_id,
                    "entity_type": "task",
                    "entity_id": task_id,
                    "event_type": "task.completed",
                    "event_payload": {"catalog": template.process_type},
                },
                start_to_close_timeout=timedelta(seconds=10),
            )

            if is_silent_system_ack(clean_result) or (
                is_heartbeat_request(user_intent) and is_heartbeat_ack(clean_result)
            ):
                return await self._finish_durable_catalog(
                    payload,
                    user_intent,
                    {"status": "completed", "task_id": task_id, "delivered": False},
                    task_kind,
                )

            summary = clean_result
            await workflow.execute_activity(
                notify_slack_user,
                {
                    "session_key": session_key,
                    "task_id": task_id,
                    "intent": user_intent,
                    "task_type": payload.get("task_type", ""),
                    "tags": payload.get("tags") or [],
                    "message": summary,
                },
                start_to_close_timeout=timedelta(seconds=30),
            )
            return await self._finish_durable_catalog(
                payload,
                user_intent,
                {"status": "completed", "task_id": task_id, "final_result": summary},
                task_kind,
            )

        except Exception as e:
            if step_context.strip():
                await workflow.execute_activity(
                    execute_compensation,
                    {
                        "task_id": task_id,
                        "process_run_id": self.process_run_id,
                        "reason": f"Workflow exception: {str(e)[:300]}",
                    },
                    start_to_close_timeout=timedelta(seconds=15),
                )
                await workflow.execute_activity(
                    notify_slack_user,
                    {
                        "session_key": session_key,
                        "task_id": task_id,
                        "message": f"Task compensated after error: {format_workflow_error(e)}",
                    },
                    start_to_close_timeout=timedelta(seconds=30),
                )
                return {"status": "compensated", "task_id": task_id}
            await workflow.execute_activity(
                update_task_status,
                {"task_id": task_id, "status": "failed"},
                start_to_close_timeout=timedelta(seconds=10),
            )
            await workflow.execute_activity(
                update_process_state,
                {
                    "process_run_id": self.process_run_id,
                    "state": "failed_terminal",
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
                    "task_type": payload.get("task_type", process_type),
                    "tags": payload.get("tags") or [],
                    "message": f"Task encountered an error: {format_workflow_error(e)}",
                },
                start_to_close_timeout=timedelta(seconds=30),
            )
            raise
