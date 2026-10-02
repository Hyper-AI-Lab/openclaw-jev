"""The coding workflow on Temporal's time-skipping server, with a scripted runner and verification.

Every activity is scripted by name, so each test reads as the story of one coding task: what Claude
Code returned, what RMP's own test run showed, what Aura and the evaluator said, and what Kirill
replied in Slack.
"""
import asyncio
import json
import os
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.coding import prompts
from app.orchestrator.process_brief import with_catchup
from app.workflows.coding_task import CodingTaskWorkflow

QUEUE = "coding-harness"
STREAMS = Path(__file__).resolve().parent / "fixtures" / "claude_streams"
SESSION = "agent:main:slack:channel:d0test"
CATCHUP = "USER CATCH-UP (attach/rebuild):\nKirill asked Aura to fix the greeting and said ok to stop the old draft."
PAYLOAD = {"task_id": "t1", "intent": "Make Aura's greeting say hello.", "session_key": SESSION, "task_type": "user",
           "tags": ["user-request"], "process_type": "coding_task"}
SETTINGS = {"enabled": True, "max_rounds": 3, "repositories": {
    "rmp": {"remote": "Hyper-AI-Lab/openclaw-jev", "deploy": "self"},
    "agentic-design": {"remote": "Hyper-AI-Lab/agentic-design", "deploy": "pr"}}}
BRIEF = {"repo": "rmp", "title": "Fix the greeting", "goal": "Make the greeting say hello.",
         "acceptance_criteria": ["Aura greets with hello"], "constraints": ["No new dependencies"], "questions": []}
BRANCH = "aura/t1-fix-the-greeting"
JOB = {"task_id": "t1", "repo": "rmp", "branch": BRANCH, "base": "b" * 40, "source": "/root/.openclaw/rmp",
       "checkout": "/srv/aura-code/jobs/t1", "review": "/srv/aura-code/review/t1.git",
       "created_at": "2026-10-02T00:00:00+00:00", "tests": [["/srv/aura-code/venvs/rmp/bin/python", "-m", "pytest", "-q"]]}
HEAD = "c" * 40
FAILING_TAIL = "FAILED tests/test_greeting.py::test_hello - AssertionError: assert 'hi' == 'hello'\n1 failed, 11 passed"
READY = {"verdict": "ready", "feedback": [], "reply": "I changed the greeting to say hello, and RMP's tests pass (12 passed)."}
FINAL = "Done: the greeting now says hello. It is deployed and verified."
RECORDED: Dict[str, Any] = {}


def claude(kind: str = "success", session: str = "sess-1", **extra: Any) -> Dict[str, Any]:
    return {"kind": kind, "session_id": session, "report": {"summary": "Changed the greeting.", "tests_passed": True},
            "num_turns": 7, "cost_usd": 0.4, "error": None, "exit": "success exited 0", "resume_at": None,
            "commands": ["pytest -q"], "files_edited": ["app/greeting.py"], "tool_counts": {"Edit": 1}, **extra}


def evidence(ok: bool = True, *, secrets: Tuple[dict, ...] = (), commits: int = 1,
             changed: Tuple[str, ...] = ("app/greeting.py",)) -> Dict[str, Any]:
    commit = {"sha": HEAD, "author": "Aura (Claude Code)", "email": "aura-coder@aura.local", "subject": "Fix the greeting"}
    return {
        "error": None,
        "collected": {"base": "b" * 40, "head": HEAD, "branch": BRANCH, "commits": [commit][:commits],
                      "diffstat": " app/greeting.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n",
                      "changed": [{"status": "M", "path": p} for p in changed], "truncated": False,
                      "secrets": list(secrets), "tests_changed": [], "dependencies_changed": []},
        "tests": {"ok": ok, "seconds": 3.0, "commands": [{
            "command": ["pytest", "-q"], "setup": False, "ok": ok, "seconds": 3.0,
            "exit": "success exited 0" if ok else "exit-code exited 1",
            "counts": {"passed": 12} if ok else {"passed": 11, "failed": 1},
            "tail": "12 passed" if ok else FAILING_TAIL}]},
    }


class CodingRun:
    """Scripted activities and what the workflow did with them."""

    def __init__(self, *, briefs=None, runs=None, evidences=None, reviews=None, verdicts=None, slot=None,
                 provenance=None, block_first_run=False, hold_first_run=False, real_activities=()):
        self.briefs = list(briefs or [BRIEF])
        self.outcomes = list(runs or [claude()])
        self.evidences = list(evidences or [evidence()])
        self.reviews = list(reviews or [READY])
        self.verdicts = list(verdicts or ["accept"] * 8)
        self.slot = list(slot or [])
        self.provenance = list(provenance or [{"ok": True, "message_id": "m1"}])
        self.block_first_run = block_first_run
        self.first_run_released = None if not hold_first_run else asyncio.Event()
        self.real = {fn.__name__: fn for fn in real_activities}
        self.slack: List[Tuple[str, str]] = []
        self.runs: List[dict] = []
        self.brief_calls: List[dict] = []
        self.verified: List[dict] = []
        self.reviewed: List[dict] = []
        self.judged: List[dict] = []
        self.statuses: List[str] = []
        self.states: List[str] = []
        self.deploys: List[dict] = []
        self.failed: List[dict] = []
        self.released: List[str] = []
        self.provenance_calls: List[dict] = []
        self.stops: List[float] = []
        self.cancels: List[Tuple[float, bool]] = []
        self.process_type: Optional[str] = None

    def notices(self) -> List[str]:
        return [message for message, _ in self.slack]

    def cards(self) -> List[str]:
        return [m for m in self.notices() if "• Change:" in m]

    def activities(self):
        rec = self

        @activity.defn(name="coding_settings")
        async def coding_settings(payload):
            return SETTINGS

        @activity.defn(name="acquire_coding_slot")
        async def acquire_coding_slot(payload):
            return rec.slot.pop(0) if rec.slot else {"granted": True, "holder": payload["task_id"]}

        @activity.defn(name="release_coding_slot")
        async def release_coding_slot(payload):
            rec.released.append(payload["task_id"])
            return True

        @activity.defn(name="draft_coding_brief")
        async def draft_coding_brief(payload):
            rec.brief_calls.append(payload)
            return rec.briefs.pop(0)

        @activity.defn(name="prepare_coding_workspace")
        async def prepare_coding_workspace(payload):
            return JOB

        @activity.defn(name="run_claude_round")
        async def run_claude_round(payload):
            rec.runs.append(payload)
            if rec.block_first_run and len(rec.runs) == 1:
                try:
                    while True:
                        activity.heartbeat()
                        await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    details = activity.cancellation_details()
                    rec.cancels.append((time.monotonic(), bool(details and details.cancel_requested)))
                    raise
            if rec.first_run_released is not None and len(rec.runs) == 1:
                await rec.first_run_released.wait()
            return rec.outcomes.pop(0)

        @activity.defn(name="stop_coding_units")
        async def stop_coding_units(payload):
            rec.stops.append(time.monotonic())
            return ["aura-claude-t1-1"] if len(rec.stops) == 1 else []

        @activity.defn(name="verify_coding_round")
        async def verify_coding_round(payload):
            rec.verified.append(payload)
            return rec.evidences.pop(0)

        @activity.defn(name="review_coding_round")
        async def review_coding_round(payload):
            rec.reviewed.append(payload)
            return rec.reviews.pop(0)

        @activity.defn(name="verify_response_quality")
        async def verify_response_quality(payload):
            rec.judged.append(payload)
            verdict = rec.verdicts.pop(0)
            accepted = verdict == "accept"
            return {"verdict": verdict, "quality": "pass" if accepted else "fail", "reason": "",
                    "issues": "" if accepted else "the reply says the docs changed, but no doc file is in the diff",
                    "command_to_aura": "" if accepted else "Update the docs too, or stop claiming it.", "parse_error": False}

        @activity.defn(name="notify_slack_user")
        async def notify_slack_user(payload):
            rec.slack.append((payload["message"], payload.get("message_kind") or "notice"))
            return "delivered"

        @activity.defn(name="send_to_openclaw")
        async def send_to_openclaw(payload):
            return {"result": {"payloads": [{"text": FINAL}]}}

        @activity.defn(name="update_task_status")
        async def update_task_status(payload):
            rec.statuses.append(payload["status"])
            return True

        @activity.defn(name="update_process_state")
        async def update_process_state(payload):
            rec.states.append(payload["state"])
            return True

        @activity.defn(name="ensure_process_run")
        async def ensure_process_run(payload):
            rec.process_type = payload.get("process_type")
            return "run-1"

        @activity.defn(name="finalize_task_failure")
        async def finalize_task_failure(payload):
            rec.failed.append(payload)
            return True

        @activity.defn(name="confirm_approval_provenance")
        async def confirm_approval_provenance(payload):
            rec.provenance_calls.append(payload)
            return rec.provenance.pop(0)

        @activity.defn(name="resubmit_user_messages")
        async def resubmit_user_messages(payload):
            return None

        @activity.defn(name="record_event")
        async def record_event(payload):
            return "event-1"

        @activity.defn(name="deploy_coding_change")
        async def deploy_coding_change(payload):
            rec.deploys.append(payload)
            if payload["target"] == "self":
                return {"status": "deployed", "summary": f"Deployed {payload['head'][:12]} to main; health, readiness and canary passed."}
            return {"status": "pr_opened", "summary": "Opened https://github.com/Hyper-AI-Lab/agentic-design/pull/7"}

        scripted = [coding_settings, acquire_coding_slot, release_coding_slot, draft_coding_brief, prepare_coding_workspace,
                    run_claude_round, stop_coding_units, verify_coding_round, review_coding_round, verify_response_quality,
                    notify_slack_user, send_to_openclaw, update_task_status, update_process_state, ensure_process_run,
                    finalize_task_failure, confirm_approval_provenance, resubmit_user_messages, record_event,
                    deploy_coding_change]
        return [rec.real.get(fn.__name__, fn) for fn in scripted]


async def until(predicate, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


async def drive(rec: CodingRun, script=None, payload=None, *, record: Optional[str] = None) -> Dict[str, Any]:
    payload = payload or PAYLOAD
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=QUEUE, workflows=[CodingTaskWorkflow], activities=rec.activities(),
                          max_heartbeat_throttle_interval=timedelta(milliseconds=100)):
            handle = await env.client.start_workflow(CodingTaskWorkflow.run, payload, id=f"workflow-{payload['task_id']}",
                                                     task_queue=QUEUE)
            if script:
                await script(env, handle)
            result = await handle.result()
            if record and os.environ.get("RECORD_CODING_HISTORIES"):
                history = await handle.fetch_history()
                RECORDED[record] = {"workflow_id": handle.id, "history": json.loads(history.to_json())}
                path = Path(os.environ["RECORD_CODING_HISTORIES"])
                saved = json.loads(path.read_text()) if path.exists() else {}
                path.write_text(json.dumps({**saved, record: RECORDED[record]}, separators=(",", ":")) + "\n")
            return result


def kirill(words: str) -> str:
    return with_catchup(CATCHUP, words)


async def approve_first_card(env, handle, rec: CodingRun) -> None:
    await until(lambda: rec.cards())
    await handle.signal("user_input", kirill("approve"))


async def test_an_approved_change_to_auras_own_code_deploys_and_gets_a_judged_reply():
    rec = CodingRun()
    result = await drive(rec, lambda env, handle: approve_first_card(env, handle, rec), record="approve_and_deploy")

    assert result["status"] == "completed" and rec.process_type == "coding_task"
    assert any(m.startswith(f"Starting on rmp: Fix the greeting.") and BRANCH in m for m in rec.notices())
    card = rec.cards()[0]
    assert card.startswith(READY["reply"])
    for fact in (f"1 commit(s) on {BRANCH}, head {HEAD[:12]}", "Tests (RMP's own run): passed (12 passed)",
                 "Diffstat: 1 file changed, 1 insertion(+), 1 deletion(-)", "Restarts on deploy: rmp-api, rmp-worker",
                 "Dependency changes: none", "Reply approve to deploy it"):
        assert fact in card
    assert rec.runs[0]["resume_session"] is None and rec.runs[0]["prompt"].startswith(BRIEF["goal"])
    assert "Hyper-AI-Lab/openclaw-jev" in rec.runs[0]["system_prompt"] and "pytest" in rec.runs[0]["system_prompt"]
    assert rec.judged[0]["external_evidence"]["tests"]["ok"] is True
    assert rec.judged[0]["external_evidence"]["commits"][0]["sha"] == HEAD
    assert rec.provenance_calls and rec.deploys[0]["head"] == HEAD and rec.deploys[0]["target"] == "self"
    assert rec.judged[-1]["agent_response"] == FINAL and rec.judged[-1]["external_evidence"]["deploy"]["status"] == "deployed"
    assert rec.slack[-1] == (FINAL, "reply")
    assert rec.statuses[-1] == "completed" and rec.states[-1] == "completed" and rec.released == ["t1"]


async def test_failing_tests_and_review_feedback_send_claude_back_into_its_session():
    rework = {"verdict": "rework", "feedback": ["Also update the greeting's docstring."], "reply": "Not done yet."}
    rec = CodingRun(runs=[claude(), claude(), claude()], evidences=[evidence(ok=False), evidence(), evidence()],
                    reviews=[rework, READY])
    result = await drive(rec, lambda env, handle: approve_first_card(env, handle, rec), record="rework_then_approve")

    assert result["status"] == "completed" and len(rec.runs) == 3 and len(rec.cards()) == 1
    assert "Round 1: RMP's test run failed. Claude Code is working on it again." in rec.notices()
    second, third = rec.runs[1], rec.runs[2]
    assert second["resume_session"] == "sess-1" and FAILING_TAIL.splitlines()[0] in second["prompt"]
    assert len(rec.reviewed) == 2, "a round RMP's own record already fails is not sent to review"
    assert third["resume_session"] == "sess-1" and "Also update the greeting's docstring." in third["prompt"]
    assert "Round 2: my review found more to do. Claude Code is working on it again." in rec.notices()
    assert [r["round"] for r in rec.runs] == [1, 2, 3] and [v["number"] for v in rec.verified] == [1, 2, 3]


async def test_kirills_messages_during_a_round_reach_the_next_one():
    rec = CodingRun(runs=[claude(), claude()], evidences=[evidence(ok=False), evidence()], hold_first_run=True)

    async def script(env, handle):
        await until(lambda: rec.runs)
        await handle.signal("user_input", kirill("Keep the old greeting as a fallback."))
        await asyncio.sleep(0.2)
        rec.first_run_released.set()
        await approve_first_card(env, handle, rec)

    await drive(rec, script)
    assert "Kirill added: Keep the old greeting as a fallback." in rec.runs[1]["prompt"]
    assert rec.brief_calls[0]["answers"] == []


async def test_a_stop_during_the_run_stops_the_unit_within_seconds_and_ships_nothing():
    rec = CodingRun(block_first_run=True)
    sent: List[float] = []

    async def script(env, handle):
        await until(lambda: rec.runs)
        sent.append(time.monotonic())
        await handle.signal("user_input", kirill("stop"))

    result = await drive(rec, script, record="stop_mid_run")

    assert result["status"] == "stopped_by_user"
    assert rec.stops and rec.stops[0] - sent[0] < 5, "the unit is stopped within seconds of Kirill's stop"
    assert rec.cancels and rec.cancels[0][1] is True and rec.stops[0] <= rec.cancels[0][0]
    assert not rec.verified and not rec.deploys and not rec.provenance_calls
    assert rec.notices()[-1] == f"Stopped. Nothing shipped. The work so far is kept on branch {BRANCH}."
    assert rec.statuses[-1] == "stopped_by_user" and rec.states[-1] == "stopped_by_user" and rec.released == ["t1"]


async def test_a_stop_at_the_gate_ships_nothing():
    rec = CodingRun()

    async def script(env, handle):
        await until(lambda: rec.cards())
        await handle.signal("user_input", kirill("stop"))

    result = await drive(rec, script)
    assert result["status"] == "stopped_by_user" and not rec.deploys and not rec.provenance_calls
    assert rec.statuses[-1] == "stopped_by_user"


async def test_a_change_request_at_the_gate_starts_another_round_in_the_same_session():
    second_reply = {**READY, "reply": "The greeting now says hi there, and RMP's tests pass (12 passed)."}
    rec = CodingRun(runs=[claude(), claude()], evidences=[evidence(), evidence()], reviews=[READY, second_reply])

    async def script(env, handle):
        await until(lambda: rec.cards())
        await handle.signal("user_input", kirill("Please make it say hi there instead."))
        await until(lambda: len(rec.cards()) == 2)
        await handle.signal("user_input", kirill("approve"))

    result = await drive(rec, script, record="change_request_then_approve")

    assert result["status"] == "completed" and len(rec.deploys) == 1
    assert rec.runs[1]["resume_session"] == "sess-1" and "Please make it say hi there instead." in rec.runs[1]["prompt"]
    assert rec.cards()[1].startswith(second_reply["reply"])
    assert len(rec.provenance_calls) == 1, "only an approval is checked for provenance"


async def test_an_approval_sent_with_a_change_request_does_not_ship():
    rec = CodingRun(runs=[claude(), claude()], evidences=[evidence(), evidence()], reviews=[READY, READY],
                    hold_first_run=True)

    async def script(env, handle):
        await until(lambda: rec.runs)
        await handle.signal("user_input", kirill("approve"))
        await handle.signal("user_input", kirill("Also rename the flag to greeting_word."))
        rec.first_run_released.set()
        await until(lambda: len(rec.cards()) == 2)
        assert not rec.provenance_calls and not rec.deploys
        await handle.signal("user_input", kirill("approve"))

    result = await drive(rec, script)
    assert result["status"] == "completed" and len(rec.provenance_calls) == 1 and len(rec.deploys) == 1
    assert "Also rename the flag to greeting_word." in rec.runs[1]["prompt"] and "approve" not in rec.runs[1]["prompt"]


async def test_an_approval_without_kirills_slack_message_is_refused():
    rec = CodingRun(provenance=[{"ok": False, "reason": "no Slack approval after the gate opened"}, {"ok": True}])

    async def script(env, handle):
        await until(lambda: rec.cards())
        await handle.signal("approve", "approve")
        await until(lambda: any("only ship this on your own approve" in m for m in rec.notices()))
        assert not rec.deploys
        await handle.signal("user_input", kirill("approve"))

    result = await drive(rec, script)
    assert result["status"] == "completed" and len(rec.provenance_calls) == 2 and len(rec.deploys) == 1
    assert rec.provenance_calls[0]["gate_opened_at"] == rec.provenance_calls[1]["gate_opened_at"]


async def test_the_usage_limit_pauses_until_the_reset_and_resumes_the_session():
    resets = int(time.time()) + 2 * 3600
    rec = CodingRun(runs=[claude("usage_limit", resume_at=resets + 60, error="rate limited"), claude()])

    async def script(env, handle):
        await until(lambda: any("reached its usage limit" in m for m in rec.notices()))
        assert rec.statuses[-1] == "blocked" and rec.states[-1] == "paused" and len(rec.runs) == 1
        await env.sleep(timedelta(hours=1))
        assert len(rec.runs) == 1
        await env.sleep(timedelta(hours=1, minutes=5))
        await until(lambda: len(rec.runs) == 2)
        await approve_first_card(env, handle, rec)

    result = await drive(rec, script, record="usage_limit_pause")

    assert result["status"] == "completed"
    assert rec.runs[1]["resume_session"] == "sess-1" and rec.runs[1]["prompt"] == prompts.CONTINUE_PROMPT
    assert rec.runs[1]["number"] == 2 and rec.runs[1]["round"] == 1, "the continuation is a new run in the same round"


async def test_a_stop_during_the_usage_limit_pause_ends_the_task():
    rec = CodingRun(runs=[claude("usage_limit", resume_at=int(time.time()) + 3 * 3600)])

    async def script(env, handle):
        await until(lambda: any("reached its usage limit" in m for m in rec.notices()))
        await handle.signal("user_input", kirill("stop"))

    result = await drive(rec, script)
    assert result["status"] == "stopped_by_user" and len(rec.runs) == 1


async def test_a_runner_failure_fails_the_task_and_keeps_the_checkout():
    rec = CodingRun(runs=[claude("auth_failed", error="OAuth token has expired")])
    result = await drive(rec)

    assert result["status"] == "failed" and rec.failed and not rec.verified and not rec.deploys
    notice = rec.notices()[-1]
    assert "token needs renewing" in notice and "OAuth token has expired" in notice and BRANCH in notice
    assert rec.released == ["t1"]


async def test_a_busy_slot_queues_the_task_with_one_notice():
    busy = {"granted": False, "holder": "other-task-1234"}
    rec = CodingRun(slot=[busy, busy])

    async def script(env, handle):
        await until(lambda: any("Another coding job is running (task other-ta)" in m for m in rec.notices()))
        assert rec.statuses[-1] == "blocked" and not rec.brief_calls
        await env.sleep(timedelta(minutes=5))
        await until(lambda: rec.cards())
        await handle.signal("user_input", kirill("stop"))

    await drive(rec, script)
    assert sum("Another coding job is running" in m for m in rec.notices()) == 1
    assert "running" in rec.statuses[rec.statuses.index("blocked"):]


async def test_questions_in_the_brief_go_to_kirill_and_his_answer_comes_back():
    asking = {**BRIEF, "repo": None, "questions": ["Which greeting: the Slack one or the web one?"]}
    rec = CodingRun(briefs=[asking, BRIEF])

    async def script(env, handle):
        await until(lambda: any(m.startswith("Before I start on this, I need to know:") for m in rec.notices()))
        assert rec.statuses[-1] == "pending_user_input" and len(rec.brief_calls) == 1
        await handle.signal("user_input", kirill("The Slack one."))
        await until(lambda: rec.cards())
        await handle.signal("user_input", kirill("stop"))

    await drive(rec, script)
    assert rec.brief_calls[1]["answers"] == ["The Slack one."]


async def test_a_change_that_is_not_ready_after_the_last_round_cannot_be_approved():
    rec = CodingRun(runs=[claude()] * 3, evidences=[evidence(ok=False)] * 3)

    async def script(env, handle):
        await until(lambda: rec.cards())
        await handle.signal("user_input", kirill("approve"))
        await until(lambda: any(m.startswith("I can't ship this yet") for m in rec.notices()))
        await handle.signal("user_input", kirill("stop"))

    result = await drive(rec, script)
    assert result["status"] == "stopped_by_user" and not rec.provenance_calls and not rec.deploys
    card = rec.cards()[0]
    assert card.startswith("After 3 round(s) this isn't ready to ship:\n- RMP's test run failed")
    assert "Tests (RMP's own run): FAILED (1 failed, 11 passed)" in card and "Reply approve" not in card


async def test_a_secret_in_the_diff_is_never_shipped_and_goes_back_to_claude():
    found = ({"path": "app/greeting.py", "diff_line": 7, "kind": "GitHub token"},)
    rec = CodingRun(runs=[claude(), claude()], evidences=[evidence(secrets=found), evidence()])
    await drive(rec, lambda env, handle: approve_first_card(env, handle, rec))
    assert "GitHub token in app/greeting.py" in rec.runs[1]["prompt"] and len(rec.reviewed) == 1


async def test_a_worker_restart_mid_run_reattaches_to_the_same_unit(tmp_path, monkeypatch):
    """The real round activity on a scripted runner: the unit outlives the worker and is never started twice."""
    from app.activities import coding_activities
    from app.coding import runner

    lines = (STREAMS / "edit_and_test.jsonl").read_text().splitlines()
    exit_line = (STREAMS / "edit_and_test.exit").read_text()
    starts: List[int] = []
    alive_at_reattach: List[bool] = []
    stops: List[str] = []
    units: Dict[str, threading.Thread] = {}
    reattached = threading.Event()

    def start(spec, cfg):
        run = runner.Run(spec.task_id, spec.number, tmp_path)
        starts.append(activity.info().attempt)
        if run.unit in units:
            alive_at_reattach.append(units[run.unit].is_alive())
            reattached.set()
            return run
        run.dir.mkdir(parents=True)

        def write():
            with run.stream_file.open("a") as fh:
                for index, line in enumerate(lines):
                    if index == 3:
                        reattached.wait(60)
                    fh.write(line + "\n")
                    fh.flush()
                    time.sleep(0.05)
            run.exit_file.write_text(exit_line)

        units[run.unit] = threading.Thread(target=write, daemon=True)
        units[run.unit].start()
        return run

    monkeypatch.setattr(runner, "start", start)
    monkeypatch.setattr(runner, "unit_active", lambda unit: unit in units and units[unit].is_alive())
    monkeypatch.setattr(runner, "stop", lambda run, **kwargs: stops.append(run.unit))
    monkeypatch.setattr(runner, "record_usage", lambda run, result: True)
    monkeypatch.setattr(coding_activities, "_touch", AsyncMock())
    stream_file = tmp_path / "t1" / "1" / "stream.jsonl"

    rec = CodingRun(real_activities=[coding_activities.run_claude_round])
    # A real dev server: the time-skipping one never retries an attempt whose worker shut down.
    async with await WorkflowEnvironment.start_local() as env:
        kwargs = dict(task_queue=QUEUE, workflows=[CodingTaskWorkflow], activities=rec.activities(),
                      max_heartbeat_throttle_interval=timedelta(milliseconds=100))
        first = Worker(env.client, **kwargs)
        running = asyncio.create_task(first.run())
        handle = await env.client.start_workflow(CodingTaskWorkflow.run, PAYLOAD, id="workflow-t1", task_queue=QUEUE)
        await until(lambda: stream_file.exists() and len(stream_file.read_text().splitlines()) >= 3)
        await first.shutdown()
        await running
        assert units["aura-claude-t1-1"].is_alive(), "the run outlives the worker"
        async with Worker(env.client, **kwargs):
            await until(lambda: rec.cards(), timeout=60)
            await handle.signal("user_input", kirill("stop"))
            assert (await handle.result())["status"] == "stopped_by_user"

    assert starts == [1, 2] and alive_at_reattach == [True] and len(units) == 1 and not stops
    reviewed = rec.reviewed[0]["claude"]
    assert reviewed["kind"] == "success" and reviewed["session_id"] == "f1c0e0b3-dfee-4ad2-b2cd-398dbd21bfcd"
    assert reviewed["num_turns"] == 4 and len(reviewed["commands"]) == 2, "the retry reads the whole run, not just its tail"


async def test_a_pull_request_repo_gets_its_own_card_and_no_restarts():
    rec = CodingRun(briefs=[{**BRIEF, "repo": "agentic-design"}])
    result = await drive(rec, lambda env, handle: approve_first_card(env, handle, rec))
    card = rec.cards()[0]
    assert "Reply approve to push the branch and open a pull request" in card and "Restarts on deploy" not in card
    assert result["shipped"]["status"] == "pr_opened" and rec.deploys[0]["target"] == "pr"
