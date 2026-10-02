"""Coding activities: the coding slot, stopping a task's units, the round's cancellation, verification, the brief."""
import asyncio
import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.activity import ActivityCancellationDetails
from temporalio.testing import ActivityEnvironment

from app.activities import coding_activities as ca
from app.coding import runner, workspace
from app.coding.units import systemd_run_argv

FAKES = Path(__file__).resolve().parent / "fakes" / "coding"


@pytest.fixture
def slot(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "SLOT_FILE", tmp_path / "coding-slot.json")
    active = {}

    async def task_active(task_id):
        return active.get(task_id, False)

    monkeypatch.setattr(ca, "_task_active", task_active)
    return active


async def test_one_coding_job_holds_the_slot_until_it_releases_it(slot):
    slot.update(a=True, b=True)
    assert (await ca.acquire_coding_slot({"task_id": "a"}))["granted"]
    assert await ca.acquire_coding_slot({"task_id": "b"}) == {"granted": False, "holder": "a"}
    assert (await ca.acquire_coding_slot({"task_id": "a"}))["granted"], "a retried acquire keeps the slot"
    assert not await ca.release_coding_slot({"task_id": "b"}), "only the holder releases it"
    assert await ca.release_coding_slot({"task_id": "a"})
    assert (await ca.acquire_coding_slot({"task_id": "b"}))["granted"]


async def test_a_holder_whose_task_ended_loses_the_slot(slot):
    slot.update(a=True, b=True)
    await ca.acquire_coding_slot({"task_id": "a"})
    slot["a"] = False
    assert (await ca.acquire_coding_slot({"task_id": "b"}))["granted"] and ca.slot_holder() == "b"


@pytest.fixture
def units(tmp_path, monkeypatch):
    state = tmp_path / "units"
    monkeypatch.setenv("PATH", f"{FAKES}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_UNITS_DIR", str(state))
    monkeypatch.setattr(ca, "RUNS_DIR", tmp_path / "runs")

    def start(unit):
        subprocess.run(systemd_run_argv(unit, ["sleep", "60"], properties=[f"StandardOutput=file:{tmp_path / unit}.log"],
                                        workdir=tmp_path), check=True, capture_output=True)

    yield SimpleNamespace(start=start, runs=tmp_path / "runs")
    for pid_file in state.glob("*.pid"):
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def test_stopping_a_task_stops_its_claude_run_with_the_stop_recorded_and_its_test_units(units):
    run = runner.Run("t1", 1, units.runs)
    run.dir.mkdir(parents=True)
    for unit in (run.unit, "aura-verify-t1-1-0", "aura-claude-t2-1"):
        units.start(unit)
    assert sorted(ca.live_task_units("t1")) == ["aura-claude-t1-1.service", "aura-verify-t1-1-0.service"]

    assert ca.stop_task_units("t1") == ["aura-claude-t1-1", "aura-verify-t1-1-0.service"]
    assert run.stop_file.exists(), "the run reads as stopped, not failed"
    assert ca.live_task_units("t1") == [] and runner.unit_active("aura-claude-t2-1"), "other tasks' units keep running"


@pytest.fixture
def endless_run(tmp_path, monkeypatch):
    """A round whose unit never ends, and a record of what stopped it."""
    stopped = []
    monkeypatch.setattr(runner, "start", lambda spec, cfg: runner.Run(spec.task_id, spec.number, tmp_path))
    monkeypatch.setattr(runner, "status", lambda run: {"active": True, "exit": None, "stopped": False})
    monkeypatch.setattr(runner, "stop", lambda run, **kwargs: stopped.append(run.unit))
    monkeypatch.setattr(ca, "_touch", AsyncMock())
    monkeypatch.setattr(ca, "POLL_SEC", 0.01)
    return stopped


ROUND = {"task_id": "t1", "number": 1, "round": 1, "checkout": "/srv/aura-code/jobs/t1", "prompt": "Fix the greeting."}


@pytest.mark.parametrize("details, stops", [
    (ActivityCancellationDetails(cancel_requested=True), ["aura-claude-t1-1"]),
    (ActivityCancellationDetails(worker_shutdown=True), []),
    (ActivityCancellationDetails(timed_out=True), []),
])
async def test_only_a_cancel_the_workflow_asked_for_stops_the_run(endless_run, details, stops):
    env = ActivityEnvironment()
    running = asyncio.ensure_future(env.run(ca.run_claude_round, dict(ROUND)))
    await asyncio.sleep(0.1)
    env.cancel(details)
    with pytest.raises(asyncio.CancelledError):
        await running
    assert endless_run == stops


@pytest.fixture
def verify_env(units, monkeypatch):
    monkeypatch.setattr(ca, "_touch", AsyncMock())
    job = {"task_id": "t1", "repo": "rmp", "branch": "aura/t1-x", "base": "b" * 40, "source": "/src", "checkout": "/jobs/t1",
           "review": "/review/t1.git", "created_at": "2026-10-02T00:00:00+00:00", "tests": [["pytest"]]}
    (units.runs / "t1").mkdir(parents=True)
    return {"task_id": "t1", "number": 2, "job": job, "message": "Fix (round 2)"}


async def test_verification_keeps_the_diff_on_disk_and_out_of_the_result(verify_env, units, monkeypatch):
    collected = {"head": "c" * 40, "commits": [{"sha": "c" * 40}], "diff": "+hello\n", "secrets": []}
    monkeypatch.setattr(workspace, "collect", lambda job, message, cfg, number: dict(collected))
    monkeypatch.setattr(ca.verify, "run_tests", lambda job, cfg, attempt: {"ok": True, "commands": [], "attempt": attempt})

    result = await ActivityEnvironment().run(ca.verify_coding_round, verify_env)

    assert result["error"] is None and "diff" not in result["collected"] and result["tests"]["attempt"] == 2
    assert (units.runs / "t1" / "diff-2.patch").read_text() == "+hello\n"


async def test_work_rmp_cannot_collect_is_reported_and_not_tested(verify_env, monkeypatch):
    def collect(job, message, cfg, number):
        raise RuntimeError("the work no longer builds on bbbbbbbbbbbb")

    tests = []
    monkeypatch.setattr(workspace, "collect", collect)
    monkeypatch.setattr(ca.verify, "run_tests", lambda *args, **kwargs: tests.append(args))

    result = await ActivityEnvironment().run(ca.verify_coding_round, verify_env)
    assert result == {"error": "the work no longer builds on bbbbbbbbbbbb", "collected": None, "tests": None} and not tests


async def test_an_unreadable_brief_is_asked_for_again_then_given_up(monkeypatch):
    from app.activities import openclaw_activities

    replies = ["Sure! I'll get Claude on it.",
               '{"repo": "rmp", "title": "Fix greeting", "goal": "Say hello.", "acceptance_criteria": ["hello"]}']
    sent = []

    async def send(payload):
        sent.append(payload["message"])
        return {"result": {"payloads": [{"text": replies.pop(0)}]}}

    monkeypatch.setattr(openclaw_activities, "send_to_openclaw", send)
    payload = {"task_id": "t1", "intent": "Make the greeting say hello.", "answers": ["The Slack one."]}
    brief = await ca.draft_coding_brief(payload)
    assert brief["repo"] == "rmp" and brief["questions"] == [] and len(sent) == 2
    assert "could not be used (the brief was not a JSON object)" in sent[1] and "The Slack one." in sent[0]

    replies[:] = ["no", "still no"]
    assert await ca.draft_coding_brief(payload) == {"error": "the brief was not a JSON object"}
