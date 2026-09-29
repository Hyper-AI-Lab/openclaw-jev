"""Messages attached to a running task: folded into Aura's reply, never dropped."""
from datetime import timedelta
from typing import List

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.activities.intake_activities import resubmit_user_messages
    from app.activities.openclaw_activities import send_to_openclaw
    from app.notification_policy import sanitize_user_facing_text
    from app.orchestrator.process_brief import format_user_catchup
    from app.orchestrator.step_predicates import extract_agent_facts
    from app.task_registry.stop_command import is_whole_message_stop


def _is_user_message(message: str) -> bool:
    text = str(message or "").strip()
    return bool(text) and not text.startswith("[CANCEL]") and not is_whole_message_stop(text)


class AttachedMessages:
    """For task workflows whose `user_input` signal appends to `self.user_inputs`.

    Stop and cancel entries stay in `user_inputs` for the workflow's stop handling.
    """

    user_inputs: List[str]

    def _has_user_messages(self) -> bool:
        return any(_is_user_message(m) for m in self.user_inputs)

    def _take_user_messages(self) -> List[str]:
        taken = [str(m).strip() for m in self.user_inputs if _is_user_message(m)]
        self.user_inputs = [m for m in self.user_inputs if not _is_user_message(m)]
        return taken

    async def _fold_in(
        self,
        task_id: str,
        session_key: str,
        task_type: str,
        tags: List[str],
        draft: str,
        messages: List[str],
    ) -> str:
        prompt = (
            f"Kirill sent more while you were working on this.\n\n{format_user_catchup(messages)}\n\n"
            f"Your reply so far:\n{draft}\n\n"
            "Rewrite the reply so it also answers these messages. Send the whole reply."
        )
        resp = await workflow.execute_activity(
            send_to_openclaw,
            {
                "message": prompt,
                "task_id": task_id,
                "session_key": session_key,
                "task_type": task_type,
                "tags": tags,
            },
            start_to_close_timeout=timedelta(minutes=45),
        )
        try:
            text = resp["result"]["payloads"][0]["text"]
        except Exception:
            text = str(resp)
        return sanitize_user_facing_text(extract_agent_facts(text).get("body") or text)

    async def _resubmit_leftovers(self, task_id: str, session_key: str) -> None:
        await workflow.wait_condition(workflow.all_handlers_finished)
        leftover = self._take_user_messages()
        if leftover:
            await workflow.execute_activity(
                resubmit_user_messages,
                {"task_id": task_id, "session_key": session_key, "messages": leftover},
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
