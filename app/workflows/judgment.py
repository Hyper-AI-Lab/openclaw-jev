"""The Process Evaluator must judge before anything is sent. When it cannot, wait for it, not Aura."""
import asyncio
from datetime import timedelta
from typing import Any, Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from app.activities.db_activities import finalize_task_failure, record_event
    from app.activities.openclaw_activities import (
        REPLY_FILE_ATTEMPTS,
        SLACK_DELIVERED_WITH_FILES,
        SLACK_REFUSED,
        deliver_reply_files,
        notify_slack_user,
        verify_response_quality,
    )
    from app.notification_policy import is_internal_task
    from app.orchestrator.decision_engine import SLACK_DELIVERY_FAILED
    from app.task_registry.stop_command import is_whole_message_stop

# Waits between evaluator attempts that produced no verdict: about two hours in all.
EVALUATOR_RETRY_MINUTES = (1, 2, 4, 8, 15, 30, 60)
HOLD_NOTICE_AFTER_FAILURES = 2


class EvaluatorRetry:
    """For task workflows with `process_run_id`, `user_inputs` and `_cancel_requested`."""

    process_run_id: str
    user_inputs: List[str]
    _cancel_requested: bool

    def _stop_requested(self) -> bool:
        return self._cancel_requested or any(is_whole_message_stop(str(m)) for m in self.user_inputs)

    async def _deliver_final(self, notify_payload: Dict[str, Any]) -> bool:
        """Post the accepted reply. False when Slack refused it for good; the task has then failed."""
        outcome = await workflow.execute_activity(
            notify_slack_user,
            {"message_kind": "reply", "process_run_id": self.process_run_id, **notify_payload},
            start_to_close_timeout=timedelta(seconds=30),
        )
        if outcome == SLACK_DELIVERED_WITH_FILES and workflow.patched("reply-files"):
            # A file that can't go must not hold up or fail the task; the activity tells Kirill.
            try:
                await workflow.execute_activity(
                    deliver_reply_files,
                    {"task_id": notify_payload["task_id"], "session_key": notify_payload.get("session_key")},
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(maximum_attempts=REPLY_FILE_ATTEMPTS,
                                             initial_interval=timedelta(seconds=10)),
                )
            except ActivityError as exc:
                workflow.logger.warning("Reply files not sent for %s: %s", notify_payload["task_id"], exc)
        if outcome != SLACK_REFUSED:
            return True
        await workflow.execute_activity(
            finalize_task_failure,
            {
                "task_id": notify_payload["task_id"],
                "process_run_id": self.process_run_id,
                "task_status": "failed",
                "process_state": "failed_terminal",
                "closed_reason": SLACK_DELIVERY_FAILED,
            },
            start_to_close_timeout=timedelta(seconds=10),
        )
        return False

    async def _judge(
        self,
        payload: Dict[str, Any],
        *,
        session_key: str,
        user_intent: str,
        task_type: str,
        tags: List[str],
    ) -> Optional[Dict[str, Any]]:
        """The evaluator's verdict, or None when it stayed unavailable or a stop arrived."""
        failures = 0
        for wait_minutes in (*EVALUATOR_RETRY_MINUTES, None):
            try:
                quality = await workflow.execute_activity(
                    verify_response_quality,
                    payload,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except ActivityError:
                quality = {"parse_error": True}
            if not quality.get("parse_error"):
                return quality
            failures += 1
            if failures == HOLD_NOTICE_AFTER_FAILURES and not is_internal_task(user_intent, task_type, tags):
                await workflow.execute_activity(
                    notify_slack_user,
                    {
                        "session_key": session_key,
                        "task_id": payload["task_id"],
                        "intent": user_intent,
                        "task_type": task_type,
                        "tags": tags,
                        "message": (
                            "Your reply is ready, but my reviewer is unavailable right now. "
                            "I'm holding it and will send it once it has been checked."
                        ),
                    },
                    start_to_close_timeout=timedelta(seconds=30),
                )
            if wait_minutes is None or self._stop_requested():
                return None
            try:
                await workflow.wait_condition(self._stop_requested, timeout=timedelta(minutes=wait_minutes))
            except asyncio.TimeoutError:
                pass
        return None

    async def _reviewer_unavailable(
        self,
        task_id: str,
        session_key: str,
        user_intent: str,
        task_type: str,
        tags: List[str],
    ) -> Dict[str, Any]:
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
        notified = not is_internal_task(user_intent, task_type, tags)
        if notified:
            await workflow.execute_activity(
                notify_slack_user,
                {
                    "session_key": session_key,
                    "task_id": task_id,
                    "intent": user_intent,
                    "task_type": task_type,
                    "tags": tags,
                    "message": (
                        "I finished your request, but my reviewer stayed unavailable for about two hours, "
                        "so I haven't sent an unchecked answer. Please ask me again."
                    ),
                },
                start_to_close_timeout=timedelta(seconds=30),
            )
        await workflow.execute_activity(
            record_event,
            {
                "correlation_id": task_id,
                "entity_type": "task",
                "entity_id": task_id,
                "event_type": "evaluator.unavailable",
                "event_payload": {"retries": len(EVALUATOR_RETRY_MINUTES)},
            },
            start_to_close_timeout=timedelta(seconds=10),
        )
        return {
            "status": "failed",
            "task_id": task_id,
            "reason": "evaluator_unavailable",
            "user_notified": notified,
        }
