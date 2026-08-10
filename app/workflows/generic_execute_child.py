"""Child workflow: one generic plan step execution."""
from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.activities.db_activities import (
        acquire_process_run_lease,
        record_observation,
        record_step,
        release_process_run_lease,
        write_episodic_observation,
    )
    from app.activities.openclaw_activities import (
        send_to_openclaw,
        validate_openclaw_output,
    )
    from app.orchestrator.prompt_policy import build_generic_execute_prompt
    from app.orchestrator.step_predicates import decide_status_from_predicates


@workflow.defn
class GenericExecuteChildWorkflow:
    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        task_id = payload["task_id"]
        session_key = payload["session_key"]
        user_intent = payload.get("user_intent", "")
        process_run_id = payload["process_run_id"]
        step_name = payload.get("step_name", "execute")
        step_prompt = payload.get("step_prompt", "")
        predicate_id = payload.get("predicate_id", "generic_deliver")
        attempt = int(payload.get("attempt", 1))
        max_attempts = int(payload.get("max_attempts", 3))
        memory_block = payload.get("memory_block", "")
        context_block = payload.get("context_block", "")
        generic_profile = payload.get("generic_profile")
        defer_episodic_write = bool(payload.get("defer_episodic_write"))

        step_id = await workflow.execute_activity(
            record_step,
            {
                "process_run_id": process_run_id,
                "step_name": step_name,
                "step_kind": "openclaw_dispatch",
                "attempt_no": attempt,
                "idempotency_key": f"{task_id}-{step_name}-{attempt}",
                "input_ref": {"step": step_name},
            },
            start_to_close_timeout=timedelta(seconds=10),
        )

        lease = await workflow.execute_activity(
            acquire_process_run_lease,
            {"process_run_id": process_run_id, "owner": task_id},
            start_to_close_timeout=timedelta(seconds=10),
        )
        if not lease.get("acquired"):
            return {
                "status": "pending",
                "reason": f"Lease {lease.get('reason')}",
                "text": "",
                "step_id": step_id,
            }

        exec_block = context_block
        if step_prompt:
            exec_block += f"\nSTEP INSTRUCTIONS:\n{step_prompt}\n"

        prompt = build_generic_execute_prompt(
            user_intent=user_intent,
            memory_block=memory_block,
            context_block=exec_block,
            profile=generic_profile,
            user_time_block=payload.get("user_time_block") or "",
        )
        prompt += (
            "\nEnd with a fenced facts block (not inline in the user reply):\n"
            '```json\n{"facts": {"step_complete": true, "file_path": "...", "read_ok": true}}\n```\n'
        )

        try:
            execution_response = await workflow.execute_activity(
                send_to_openclaw,
                {"message": prompt, "task_id": task_id, "session_key": session_key},
                start_to_close_timeout=timedelta(minutes=45),
                retry_policy=RetryPolicy(maximum_attempts=2, backoff_coefficient=2.0),
                heartbeat_timeout=timedelta(minutes=12),
            )
            text_content = ""
            try:
                if "result" in execution_response and "payloads" in execution_response["result"]:
                    text_content = execution_response["result"]["payloads"][0]["text"]
            except Exception:
                text_content = str(execution_response)

            validation = await workflow.execute_activity(
                validate_openclaw_output,
                {"text": text_content},
                start_to_close_timeout=timedelta(seconds=10),
            )
            validation_ok = bool(validation.get("is_valid"))

            await workflow.execute_activity(
                record_observation,
                {
                    "process_run_id": process_run_id,
                    "source": "openclaw",
                    "observation_type": f"plan.{step_name}",
                    "payload": {"text": text_content[:4000], "step_id": step_id},
                },
                start_to_close_timeout=timedelta(seconds=10),
            )
            if not defer_episodic_write:
                await workflow.execute_activity(
                    write_episodic_observation,
                    {
                        "process_run_id": process_run_id,
                        "task_id": task_id,
                        "step_id": step_id,
                        "text": text_content,
                    },
                    start_to_close_timeout=timedelta(seconds=90),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )

            decision = decide_status_from_predicates(
                predicate_id,
                user_intent,
                text_content,
                validation_ok,
                attempt,
                max_attempts,
            )
            from app.notification_policy import sanitize_user_facing_text
            from app.orchestrator.step_predicates import extract_agent_facts

            display_text = sanitize_user_facing_text(
                extract_agent_facts(text_content).get("body") or text_content
            )
            step_status = "completed" if decision["action"] == "complete" else (
                "failed" if decision["action"] == "fail" else decision["status"]
            )
            await workflow.execute_activity(
                record_step,
                {
                    "process_run_id": process_run_id,
                    "step_name": step_name,
                    "idempotency_key": f"{task_id}-{step_name}-{attempt}",
                    "status": step_status,
                    "ended": True,
                },
                start_to_close_timeout=timedelta(seconds=10),
            )

            return {
                "status": decision["status"],
                "reason": decision.get("reason", ""),
                "text": display_text,
                "step_id": step_id,
                "orchestrator_action": decision.get("action"),
            }
        finally:
            await workflow.execute_activity(
                release_process_run_lease,
                {"process_run_id": process_run_id, "owner": task_id},
                start_to_close_timeout=timedelta(seconds=10),
            )
