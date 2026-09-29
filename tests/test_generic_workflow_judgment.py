"""GenericTaskWorkflow on Temporal's time-skipping server: nothing reaches Slack unjudged."""
import uuid
from typing import Any, Dict, List

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflows.generic_task import GenericTaskWorkflow

QUEUE = "judgment-tests"


class Recorder:
    def __init__(self, verdicts: List[str], reworks: List[str]):
        self.verdicts = list(verdicts)
        self.reworks = list(reworks)
        self.slack: List[str] = []
        self.judged: List[str] = []
        self.failed = 0

    def activities(self):
        rec = self

        @activity.defn(name="ensure_process_run")
        async def ensure_process_run(payload: Dict[str, Any]) -> str:
            return "pr-1"

        @activity.defn(name="record_event")
        async def record_event(payload: Dict[str, Any]) -> None:
            return None

        @activity.defn(name="update_task_status")
        async def update_task_status(payload: Dict[str, Any]) -> None:
            return None

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

        @activity.defn(name="verify_response_quality")
        async def verify_response_quality(payload: Dict[str, Any]) -> Dict[str, Any]:
            rec.judged.append(payload["agent_response"])
            verdict = rec.verdicts.pop(0) if rec.verdicts else "rework"
            quality = "pass" if verdict == "accept" else "fail"
            return {"verdict": verdict, "quality": quality, "issues": "incomplete",
                    "command_to_aura": "fix it", "reason": verdict, "parse_error": False}

        @activity.defn(name="send_to_openclaw")
        async def send_to_openclaw(payload: Dict[str, Any]) -> Dict[str, Any]:
            text = rec.reworks.pop(0) if rec.reworks else "Another attempt at the answer."
            return {"result": {"payloads": [{"text": text}]}}

        @activity.defn(name="notify_slack_user")
        async def notify_slack_user(payload: Dict[str, Any]) -> bool:
            rec.slack.append(payload["message"])
            return True

        return [ensure_process_run, record_event, update_task_status, update_process_state,
                promote_completion_memory, finalize_task_failure, execute_compensation,
                verify_response_quality, send_to_openclaw, notify_slack_user]


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


async def test_a_rejected_draft_is_reworked_and_only_the_accepted_rework_is_sent():
    rec = Recorder(verdicts=["rework", "accept"], reworks=["Layers, a rain jacket and walking shoes."])
    result = await _run(rec)
    assert result["status"] == "completed"
    assert rec.judged == ["Pack layers and a light rain jacket.", "Layers, a rain jacket and walking shoes."]
    assert rec.slack == ["Layers, a rain jacket and walking shoes."]


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
