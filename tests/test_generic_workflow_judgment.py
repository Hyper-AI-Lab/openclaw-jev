"""GenericTaskWorkflow on Temporal's time-skipping server: nothing reaches Slack unjudged."""
import uuid
from typing import Any, Dict, List

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflows.generic_task import GenericTaskWorkflow

QUEUE = "judgment-tests"


async def _signal_own_workflow(message: str) -> None:
    handle = activity.client().get_workflow_handle(activity.info().workflow_id)
    await handle.signal("user_input", message)


class Recorder:
    def __init__(self, verdicts: List[str], reworks: List[str], signals_during_judging=(), signal_on_completed=None,
                 signals_during_rework=(), slack_result: Any = "delivered"):
        self.slack_result = slack_result
        self.verdicts = list(verdicts)
        self.reworks = list(reworks)
        self.slack: List[str] = []
        self.judged: List[str] = []
        self.prompts: List[str] = []
        self.resubmitted: List[List[str]] = []
        self.failed = 0
        self.memory_builds = 0
        self.briefs: List[str] = []
        self.sessions: List[str] = []
        self.signals_during_judging = list(signals_during_judging)
        self.signal_on_completed = signal_on_completed
        self.signals_during_rework = list(signals_during_rework)
        self.events: List[Dict[str, Any]] = []
        self.kinds: List[str] = []
        self.verdict_payloads: List[Dict[str, Any]] = []
        self.before_memory = None
        self.signal_after_reply = None
        self.after_reply = None
        self.files: List[Dict[str, Any]] = []
        self.files_fail = False

    def activities(self):
        rec = self

        @activity.defn(name="ensure_process_run")
        async def ensure_process_run(payload: Dict[str, Any]) -> str:
            return "pr-1"

        @activity.defn(name="record_event")
        async def record_event(payload: Dict[str, Any]) -> None:
            rec.events.append(payload)
            return None

        @activity.defn(name="update_task_status")
        async def update_task_status(payload: Dict[str, Any]) -> None:
            if payload.get("status") == "completed" and rec.signal_on_completed:
                await _signal_own_workflow(rec.signal_on_completed)
                rec.signal_on_completed = None
            return None

        @activity.defn(name="resubmit_user_messages")
        async def resubmit_user_messages(payload: Dict[str, Any]) -> int:
            rec.resubmitted.append(list(payload["messages"]))
            return len(payload["messages"])

        @activity.defn(name="update_process_state")
        async def update_process_state(payload: Dict[str, Any]) -> None:
            return None

        @activity.defn(name="promote_completion_memory")
        async def promote_completion_memory(payload: Dict[str, Any]) -> Dict[str, Any]:
            return {}

        @activity.defn(name="finalize_task_failure")
        async def finalize_task_failure(payload: Dict[str, Any]) -> bool:
            rec.failed += 1
            return True

        @activity.defn(name="execute_compensation")
        async def execute_compensation(payload: Dict[str, Any]) -> bool:
            return True

        @activity.defn(name="build_process_memory_context")
        async def build_process_memory_context(payload: Dict[str, Any]) -> str:
            if rec.before_memory:
                await rec.before_memory(payload)
            rec.memory_builds += 1
            return ("PROCESS-SCOPED MEMORY (use this before workspace files when answering):\n\n"
                    "RECENT DIALOGUE (same Slack session — continue this conversation):\n"
                    "[05:08] Kirill: I'm off to Osaka in October.")

        @activity.defn(name="verify_response_quality")
        async def verify_response_quality(payload: Dict[str, Any]) -> Dict[str, Any]:
            rec.judged.append(payload["agent_response"])
            rec.briefs.append(payload.get("process_brief") or "")
            rec.verdict_payloads.append(payload)
            if rec.signals_during_judging:
                await _signal_own_workflow(rec.signals_during_judging.pop(0))
            verdict = rec.verdicts.pop(0) if rec.verdicts else "rework"
            if verdict == "crash":
                raise RuntimeError("evaluator model unavailable")
            if verdict == "error":
                return {"verdict": "rework", "quality": "fail", "issues": "evaluator error: malformed judge JSON",
                        "command_to_aura": "Retry", "reason": "evaluator error", "parse_error": True}
            quality = "pass" if verdict == "accept" else "fail"
            return {"verdict": verdict, "quality": quality, "issues": "incomplete",
                    "command_to_aura": "fix it", "reason": verdict, "parse_error": False}

        @activity.defn(name="task_actions_digest")
        async def task_actions_digest(payload: Dict[str, Any]) -> str:
            return '1. web_fetch {"url": "https://www.jma.go.jp"} -> ok: Osaka October forecast'

        @activity.defn(name="send_to_openclaw")
        async def send_to_openclaw(payload: Dict[str, Any]) -> Dict[str, Any]:
            rec.prompts.append(payload["message"])
            rec.sessions.append(payload.get("session_suffix") or "")
            if rec.signals_during_rework:
                await _signal_own_workflow(rec.signals_during_rework.pop(0))
            text = rec.reworks.pop(0) if rec.reworks else "Another attempt at the answer."
            return {"result": {"payloads": [{"text": text}]}}

        @activity.defn(name="notify_slack_user")
        async def notify_slack_user(payload: Dict[str, Any]) -> Any:
            rec.slack.append(payload["message"])
            rec.kinds.append(payload.get("message_kind") or "notice")
            if payload.get("message_kind") == "reply" and rec.signal_after_reply:
                await _signal_own_workflow(rec.signal_after_reply)
                rec.signal_after_reply = None
            if payload.get("message_kind") == "reply" and rec.after_reply:
                rec.after_reply()
            return rec.slack_result

        @activity.defn(name="deliver_reply_files")
        async def deliver_reply_files(payload: Dict[str, Any]) -> List[str]:
            rec.files.append(payload)
            if rec.files_fail:
                raise ApplicationError("Slack is down", non_retryable=True)
            return ["out.csv"]

        return [deliver_reply_files, ensure_process_run, record_event, update_task_status, update_process_state,
                promote_completion_memory, finalize_task_failure, execute_compensation,
                verify_response_quality, send_to_openclaw, notify_slack_user, resubmit_user_messages,
                build_process_memory_context, task_actions_digest]


async def _run(recorder: Recorder, **overrides) -> Dict[str, Any]:
    payload = {"task_id": "t1", "intent": "What should I pack for Osaka in October?",
               "session_key": "agent:main:slack:channel:d0test", "task_type": "user",
               "tags": ["user-request"], "recovered_draft": "Pack layers and a light rain jacket.",
               "rework_max_attempts": 20, "strategy_change_attempt": 10, "escalate_user_attempt": 20}
    payload.update(overrides)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=QUEUE, workflows=[GenericTaskWorkflow],
                          activities=recorder.activities()):
            return await env.client.execute_workflow(
                GenericTaskWorkflow.run, payload, id=f"wf-{uuid.uuid4()}", task_queue=QUEUE)


async def test_a_recovered_draft_is_judged_before_it_is_delivered():
    rec = Recorder(verdicts=["accept"], reworks=[])
    result = await _run(rec)
    assert result["status"] == "completed"
    assert rec.judged == ["Pack layers and a light rain jacket."]
    assert rec.slack == ["Pack layers and a light rain jacket."]
    # The recovered draft skipped the plan loop, so its memory was assembled once for the evaluator.
    assert rec.memory_builds == 1 and "Kirill: I'm off to Osaka in October." in rec.briefs[0]


async def test_files_aura_attached_go_after_the_accepted_reply_and_cannot_fail_the_task():
    rec = Recorder(verdicts=["accept"], reworks=[], slack_result="delivered_with_files")
    assert (await _run(rec))["status"] == "completed"
    assert rec.files == [{"task_id": "t1", "session_key": "agent:main:slack:channel:d0test"}]
    failing = Recorder(verdicts=["accept"], reworks=[], slack_result="delivered_with_files")
    failing.files_fail = True
    assert (await _run(failing))["status"] == "completed" and len(failing.files) == 1
    plain = Recorder(verdicts=["accept"], reworks=[])
    assert (await _run(plain))["status"] == "completed" and plain.files == []


async def test_a_delivery_result_recorded_as_a_boolean_before_sep_30_still_decodes():
    rec = Recorder(verdicts=["accept"], reworks=[], slack_result=True)
    assert (await _run(rec))["status"] == "completed"


async def test_a_rejected_draft_is_reworked_and_only_the_accepted_rework_is_sent():
    rec = Recorder(verdicts=["rework", "accept"], reworks=["Layers, a rain jacket and walking shoes."])
    result = await _run(rec)
    assert result["status"] == "completed"
    assert rec.judged == ["Pack layers and a light rain jacket.", "Layers, a rain jacket and walking shoes."]
    assert rec.slack == ["Layers, a rain jacket and walking shoes."]
    # The rework ran in its own fresh session, with everything it needs in its brief.
    assert rec.sessions == ["__r2"]
    brief = rec.prompts[0]
    assert "Attempt 2 of 20. This is a fresh session" in brief
    assert "ORIGINAL REQUEST:\nWhat should I pack for Osaka in October?" in brief
    assert "Kirill: I'm off to Osaka in October." in brief, "the memory block rides along"
    assert "ACTIONS ALREADY TAKEN IN THIS TASK" in brief and "jma.go.jp" in brief
    assert "EVALUATOR COMMAND:\nfix it" in brief and "YOUR PRIOR RESPONSE:\nPack layers and a light rain jacket." in brief


async def test_a_rework_limit_below_escalation_escalates_instead_of_sending_unjudged_work():
    rec = Recorder(verdicts=["rework", "rework"], reworks=["Second try.", "Third try, never judged."])
    result = await _run(rec, rework_max_attempts=2, escalate_user_attempt=20)
    assert result["status"] == "failed"
    assert rec.judged == ["Pack layers and a light rain jacket.", "Second try."]
    assert rec.reworks == ["Third try, never judged."]
    assert len(rec.slack) == 1 and rec.slack[0].startswith("I tried this 2 times")
    assert rec.failed == 1


async def test_escalation_sends_exactly_one_message():
    rec = Recorder(verdicts=["rework"] * 3, reworks=["b", "c"])
    result = await _run(rec, rework_max_attempts=3, strategy_change_attempt=2, escalate_user_attempt=3)
    assert result["status"] == "failed" and result["user_notified"] is True
    assert len(rec.slack) == 1


async def test_a_message_that_arrives_while_judging_is_folded_in_before_delivery():
    rec = Recorder(verdicts=["accept", "accept"], reworks=["Layers, a rain jacket and an umbrella."],
                   signals_during_judging=["also, will I need an umbrella?"])
    result = await _run(rec)
    assert result["status"] == "completed"
    assert "also, will I need an umbrella?" in rec.prompts[0]
    assert "Pack layers and a light rain jacket." in rec.prompts[0]
    assert rec.judged == ["Pack layers and a light rain jacket.", "Layers, a rain jacket and an umbrella."]
    assert rec.slack == ["Layers, a rain jacket and an umbrella."]
    assert rec.resubmitted == []


async def test_a_message_that_arrives_after_the_reply_goes_back_to_intake():
    rec = Recorder(verdicts=["accept"], reworks=[], signal_on_completed="and book the hotel, please")
    result = await _run(rec)
    assert result["status"] == "completed"
    assert rec.slack == ["Pack layers and a light rain jacket."]
    assert rec.resubmitted == [["and book the hotel, please"]]


async def test_a_stop_while_judging_stops_the_task():
    rec = Recorder(verdicts=["rework"], reworks=[], signals_during_judging=["stop"])
    result = await _run(rec)
    assert result["status"] == "stopped_by_user"
    assert rec.prompts == [] and rec.slack == ["Task t1 stopped as requested."]


async def test_a_message_that_arrives_during_a_rework_is_folded_into_the_next_judged_reply():
    rec = Recorder(verdicts=["rework", "accept"], reworks=["Layers and a rain jacket.", "Layers, jacket and good shoes."],
                   signals_during_rework=["include shoes too"])
    result = await _run(rec)
    assert result["status"] == "completed"
    assert "include shoes too" in rec.prompts[1] and "Layers and a rain jacket." in rec.prompts[1]
    assert rec.judged == ["Pack layers and a light rain jacket.", "Layers, jacket and good shoes."]
    assert rec.slack == ["Layers, jacket and good shoes."]


async def test_evaluator_failures_retry_the_evaluator_not_aura():
    rec = Recorder(verdicts=["error", "crash", "error", "accept"], reworks=[])
    result = await _run(rec)
    assert result["status"] == "completed"
    assert rec.prompts == []
    held = [m for m in rec.slack if "reviewer is unavailable" in m]
    assert len(held) == 1
    assert rec.slack[-1] == "Pack layers and a light rain jacket."


async def test_a_reviewer_that_never_recovers_closes_the_task_without_sending_the_draft():
    rec = Recorder(verdicts=["error"] * 20, reworks=[])
    result = await _run(rec)
    assert result["status"] == "failed" and result["reason"] == "evaluator_unavailable"
    assert rec.prompts == []
    assert not any("Pack layers" in m for m in rec.slack)
    assert "haven't sent an unchecked answer" in rec.slack[-1]
    assert rec.failed == 1
