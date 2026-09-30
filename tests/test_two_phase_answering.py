"""Two-phase answering on Temporal's time-skipping server.

A recall ready in time joins the memory the evaluator judges with; one that arrives after the
reply is judged for novelty, and only an accepted follow-up reaches Kirill.
"""
import asyncio
import uuid
from datetime import timedelta
from typing import Any, Dict

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.orchestrator.completion_rework import RECALL_NOTICE, RECALL_UNCONFIRMED_NOTICE
from app.workflows.catalog_task import CatalogTaskWorkflow
from app.workflows.generic_task import GenericTaskWorkflow
from tests.test_generic_workflow_judgment import Recorder

QUEUE = "two-phase-tests"
DRAFT = "Pack layers and a light rain jacket."
REPORT = {
    "relevant": True, "brief": "Kirill's Osaka trip is October 12 to 14, and he travels with carry-on only.",
    "facts": [{"statement": "Kirill travels with carry-on only.", "status": "current", "as_of": "2026-09-02",
               "citations": ["fact:f1"]}],
    "tasks": [], "sections": [], "gaps": [],
}
READY = {"status": "ready", "report_id": "r1", "report": REPORT, "latency_ms": 900}
ON = {"enabled": True, "followups": True, "deadline_sec": 180, "wait_sec": 300}
ADDS = {"verdict": "adds", "points": ["Kirill travels with carry-on only."], "reason": "a packing constraint"}
NOTHING_SETTLED = []


@workflow.defn(name="DeepRecallWorkflow")
class ScriptedRecall:
    """The recall child as a script: it waits as long as told, then returns or fails as told."""

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        script = await workflow.execute_activity(
            "recall_script", payload, start_to_close_timeout=timedelta(seconds=10)
        )
        if script["delay"]:
            await workflow.sleep(timedelta(seconds=script["delay"]))
        if script.get("fail"):
            raise ApplicationError("the recall read failed", non_retryable=True)
        return script["result"]


async def recall_completion_recorded(parent_id: str) -> None:
    """Until the parent's history holds the recall's completion, so the parent's next step sees it."""
    client = activity.client()
    await client.get_workflow_handle("t1-recall").result()
    while True:
        history = await client.get_workflow_handle(parent_id).fetch_history()
        if any(e.HasField("child_workflow_execution_completed_event_attributes") for e in history.events):
            return
        await asyncio.sleep(0.05)


class TwoPhase(Recorder):
    """The judgment harness plus a scripted recall.

    ``release`` orders the recall against the reply without relying on timing: "after_reply" lets
    it finish once the reply is posted, "now" at once, "never" not at all. While its script waits,
    an activity is running, so the test server skips no time.
    """

    def __init__(self, *, release="after_reply", recall_delay=0, recall_result=READY, recall_fail=False,
                 novelty=None, **kw):
        kw.setdefault("reworks", [])
        super().__init__(**kw)
        self.release = release
        self.script = {"delay": recall_delay, "result": recall_result, "fail": recall_fail}
        self.novelty = novelty or {"verdict": "none", "points": [], "reason": "the reply covers it"}
        self.recall_payloads = []
        self.novelty_asked = []
        self.settled = []
        self.recall_gate = None
        self.parent_id = f"wf-{uuid.uuid4()}"

    def activities(self):
        rec = self
        if rec.recall_gate is None and rec.release != "now":
            released = asyncio.Event()
            rec.recall_gate = released.wait
            if rec.release == "after_reply":
                rec.after_reply = released.set

        @activity.defn(name="recall_script")
        async def recall_script(payload: Dict[str, Any]) -> Dict[str, Any]:
            rec.recall_payloads.append(payload)
            if rec.recall_gate:
                await rec.recall_gate()
            return rec.script

        @activity.defn(name="judge_recall_novelty")
        async def judge_recall_novelty(payload: Dict[str, Any]) -> Dict[str, Any]:
            rec.novelty_asked.append(payload)
            return rec.novelty

        @activity.defn(name="settle_recall_report")
        async def settle_recall_report(payload: Dict[str, Any]) -> None:
            rec.settled.append({"consumed_by": payload["consumed_by"], "status": payload["status"]})

        return [*super().activities(), recall_script, judge_recall_novelty, settle_recall_report]


async def _run(rec: TwoPhase, **overrides) -> Dict[str, Any]:
    payload = {"task_id": "t1", "intent": "What should I pack for Osaka in October?",
               "session_key": "agent:main:slack:channel:d0test", "task_type": "user", "tags": ["user-request"],
               "recovered_draft": DRAFT, "rework_max_attempts": 20, "strategy_change_attempt": 10,
               "escalate_user_attempt": 20, "deep_recall": ON}
    payload.update(overrides)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=QUEUE, workflows=[GenericTaskWorkflow, ScriptedRecall],
                          activities=rec.activities()):
            return await env.client.execute_workflow(
                GenericTaskWorkflow.run, payload, id=rec.parent_id, task_queue=QUEUE)


async def test_a_recall_ready_before_judgment_joins_the_evaluators_brief_and_sends_no_follow_up():
    rec = TwoPhase(verdicts=["accept"], release="now")
    rec.before_memory = lambda payload: recall_completion_recorded(rec.parent_id)
    result = await _run(rec)
    assert result["status"] == "completed" and "followup" not in result
    assert "DEEP RECALL" in rec.briefs[0] and "carry-on only" in rec.briefs[0]
    assert rec.slack == [DRAFT] and rec.kinds == ["reply"]
    assert rec.novelty_asked == [] and rec.settled == [{"consumed_by": "evaluator", "status": ""}]
    assert rec.recall_payloads[0]["query"] == "What should I pack for Osaka in October?"
    assert rec.recall_payloads[0]["report_id"]


async def test_a_recall_that_adds_brings_a_notice_then_a_judged_follow_up():
    followup = "Since you travel carry-on only: layers and a packable jacket."
    rec = TwoPhase(verdicts=["accept", "accept"], reworks=[followup], novelty=ADDS)
    result = await _run(rec)
    assert result["status"] == "completed" and result["followup"] == followup
    assert rec.slack == [DRAFT, RECALL_NOTICE, followup] and rec.kinds == ["reply", "notice", "followup"]
    assert rec.sessions == ["__recall"]
    refine = rec.prompts[0]
    assert f"YOUR REPLY (already sent to Kirill):\n{DRAFT}" in refine
    assert "WHAT YOUR MEMORY ADDS:\n- Kirill travels with carry-on only." in refine
    assert "DEEP RECALL" in refine and "jma.go.jp" in refine
    assert rec.judged == [DRAFT, followup]
    assert "FOLLOW-UP:" in rec.briefs[1] and f"EARLIER REPLY (sent):\n{DRAFT}" in rec.briefs[1]
    assert [p["attempt"] for p in rec.verdict_payloads] == [1, 2]
    assert rec.novelty_asked[0]["reply"] == DRAFT and rec.novelty_asked[0]["report"] == REPORT
    assert rec.settled == [{"consumed_by": "followup", "status": ""}]
    outcomes = [e["event_payload"] for e in rec.events if e["event_type"] == "deep_recall.followup"]
    assert outcomes == [{"report_id": rec.recall_payloads[0]["report_id"], "outcome": "delivered", "attempt": 2}]


async def test_a_recall_that_corrects_asks_for_a_correction():
    correction = "Correction: your trip is October 12 to 14, so pack for early autumn."
    rec = TwoPhase(verdicts=["accept", "accept"], reworks=[correction],
                   novelty={"verdict": "corrects", "points": ["The trip is October 12 to 14."], "reason": "dates"})
    result = await _run(rec)
    assert result["followup"] == correction and rec.kinds == ["reply", "notice", "followup"]
    assert "WHAT YOUR MEMORY CORRECTS:\n- The trip is October 12 to 14." in rec.prompts[0]
    assert "needed a correction" in rec.briefs[1]


async def test_a_recall_with_nothing_new_ends_the_task_quietly():
    rec = TwoPhase(verdicts=["accept"])
    result = await _run(rec)
    assert result["status"] == "completed" and "followup" not in result
    assert rec.slack == [DRAFT] and rec.prompts == [] and len(rec.novelty_asked) == 1
    assert rec.settled == [{"consumed_by": "none", "status": ""}]


async def test_an_irrelevant_report_is_not_judged_for_novelty():
    rec = TwoPhase(verdicts=["accept"], recall_result={"status": "empty", "report_id": "r1", "report": {}})
    await _run(rec)
    assert rec.novelty_asked == [] and rec.slack == [DRAFT]
    assert rec.settled == [{"consumed_by": "none", "status": ""}]


async def test_a_failed_recall_changes_nothing_for_kirill():
    rec = TwoPhase(verdicts=["accept"], recall_fail=True)
    result = await _run(rec)
    assert result["status"] == "completed" and rec.slack == [DRAFT]
    assert rec.novelty_asked == [] and rec.settled == [{"consumed_by": "none", "status": "failed"}]


async def test_a_recall_past_its_deadline_is_closed_as_failed():
    rec = TwoPhase(verdicts=["accept"], recall_delay=600)
    result = await _run(rec, deep_recall={**ON, "deadline_sec": 60})
    assert result["status"] == "completed" and rec.slack == [DRAFT]
    assert rec.settled == [{"consumed_by": "none", "status": "failed"}]


async def test_a_recall_slower_than_the_follow_up_wait_is_let_go():
    rec = TwoPhase(verdicts=["accept"], recall_delay=400)
    result = await _run(rec, deep_recall={**ON, "deadline_sec": 600})
    assert result["status"] == "completed" and rec.slack == [DRAFT]
    assert rec.settled == [{"consumed_by": "none", "status": "cancelled"}]


async def test_a_stop_during_the_wait_ends_the_task_without_a_follow_up():
    rec = TwoPhase(verdicts=["accept"], novelty=ADDS, release="never")
    rec.signal_after_reply = "stop"
    result = await _run(rec)
    assert result["status"] == "stopped_by_user"
    assert rec.slack == [DRAFT, "Task t1 stopped as requested."]
    assert rec.novelty_asked == [] and rec.settled == [{"consumed_by": "none", "status": "cancelled"}]


async def test_a_message_during_the_wait_goes_back_to_intake_and_the_recall_is_let_go():
    rec = TwoPhase(verdicts=["accept"], novelty=ADDS, release="never")
    rec.signal_after_reply = "and book the hotel, please"
    result = await _run(rec)
    assert result["status"] == "completed" and rec.slack == [DRAFT]
    assert rec.resubmitted == [["and book the hotel, please"]]
    assert rec.novelty_asked == [] and rec.settled == [{"consumed_by": "none", "status": "cancelled"}]


async def test_a_follow_up_the_evaluator_never_accepts_is_not_sent():
    rec = TwoPhase(verdicts=["accept", "rework", "rework", "rework"],
                   reworks=["refined 1", "refined 2", "refined 3"], novelty=ADDS)
    result = await _run(rec)
    assert result["status"] == "completed" and "followup" not in result
    assert rec.slack == [DRAFT, RECALL_NOTICE, RECALL_UNCONFIRMED_NOTICE]
    assert rec.sessions == ["__recall", "__r3", "__r4"]
    assert rec.judged[1:] == ["refined 1", "refined 2", "refined 3"]
    assert rec.settled == [{"consumed_by": "none", "status": ""}]


async def test_an_internal_task_never_recalls():
    rec = TwoPhase(verdicts=["accept"])
    result = await _run(rec, task_type="canary", tags=["canary"], intent="RMP CANARY: Reply with exactly CANARY_OK")
    assert result["status"] == "completed"
    assert rec.recall_payloads == [] and rec.settled == NOTHING_SETTLED


async def test_with_recall_off_no_recall_runs():
    rec = TwoPhase(verdicts=["accept"])
    result = await _run(rec, deep_recall={**ON, "enabled": False})
    assert result["status"] == "completed" and rec.slack == [DRAFT]
    assert rec.recall_payloads == [] and rec.settled == NOTHING_SETTLED


async def test_with_follow_ups_off_a_late_recall_is_let_go_after_the_reply():
    rec = TwoPhase(verdicts=["accept"], novelty=ADDS)
    result = await _run(rec, deep_recall={**ON, "followups": False})
    assert result["status"] == "completed" and rec.slack == [DRAFT] and rec.novelty_asked == []
    assert rec.settled == [{"consumed_by": "none", "status": "cancelled"}]


@workflow.defn(name="CatalogStepChildWorkflow")
class ScriptedStep:
    """A catalog step that succeeds, after the step script has seen what it was given."""

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        await workflow.execute_activity("step_script", payload, start_to_close_timeout=timedelta(seconds=30))
        return {"status": "completed", "reason": "ok", "text": f"{payload['step_name']}: logged in, session active."}


class CatalogRun(TwoPhase):
    def __init__(self, *, recall_during_first_step=False, **kw):
        super().__init__(**kw)
        self.step_memory: Dict[str, str] = {}
        self.recall_during_first_step = recall_during_first_step

    def activities(self):
        rec = self
        first_step_started = asyncio.Event()
        if rec.recall_during_first_step:
            rec.recall_gate = first_step_started.wait

        @activity.defn(name="step_script")
        async def step_script(payload: Dict[str, Any]) -> None:
            rec.step_memory[payload["step_name"]] = payload["memory_block"]
            if rec.recall_during_first_step and len(rec.step_memory) == 1:
                first_step_started.set()
                await recall_completion_recorded(rec.parent_id)

        @activity.defn(name="write_process_memory")
        async def write_process_memory(payload: Dict[str, Any]) -> str:
            return "m1"

        @activity.defn(name="check_intermediate_updates_enabled")
        async def check_intermediate_updates_enabled(payload: Dict[str, Any]) -> bool:
            return False

        @activity.defn(name="register_artifact")
        async def register_artifact(payload: Dict[str, Any]) -> Dict[str, Any]:
            return {"skipped": True}

        return [*super().activities(), step_script, write_process_memory, check_intermediate_updates_enabled,
                register_artifact]


async def _run_catalog(rec: CatalogRun, **overrides) -> Dict[str, Any]:
    payload = {"task_id": "t1", "intent": "Log in to my example-shop.com account.", "process_type": "login",
               "session_key": "agent:main:slack:channel:d0test", "task_type": "login", "tags": ["user-request"],
               "rework_max_attempts": 20, "strategy_change_attempt": 10, "escalate_user_attempt": 20,
               "deep_recall": ON}
    payload.update(overrides)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=QUEUE, workflows=[CatalogTaskWorkflow, ScriptedStep, ScriptedRecall],
                          activities=rec.activities()):
            return await env.client.execute_workflow(
                CatalogTaskWorkflow.run, payload, id=rec.parent_id, task_queue=QUEUE)


async def test_a_catalog_task_reads_the_recall_from_the_next_step_boundary():
    rec = CatalogRun(verdicts=["accept"], recall_delay=0, recall_during_first_step=True)
    result = await _run_catalog(rec)
    assert result["status"] == "completed"
    assert list(rec.step_memory) == ["inspect_login", "submit_login", "verify_session"]
    assert "DEEP RECALL" not in rec.step_memory["inspect_login"]
    assert "carry-on only" in rec.step_memory["submit_login"] and "carry-on only" in rec.step_memory["verify_session"]
    assert "DEEP RECALL" in rec.briefs[0]
    assert rec.settled == [{"consumed_by": "steps", "status": ""}]
    assert rec.kinds == ["reply"] and rec.novelty_asked == []


async def test_a_catalog_task_lets_a_late_recall_go_without_a_follow_up():
    rec = CatalogRun(verdicts=["accept"], recall_delay=600, novelty=ADDS)
    result = await _run_catalog(rec, deep_recall={**ON, "deadline_sec": 900})
    assert result["status"] == "completed" and rec.kinds == ["reply"]
    assert not any("DEEP RECALL" in m for m in rec.step_memory.values())
    assert rec.novelty_asked == [] and rec.settled == [{"consumed_by": "none", "status": "cancelled"}]
