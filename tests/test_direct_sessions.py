"""Aura's direct Claude sessions against fake systemd-run, systemctl and claude (tests/fakes/coding)."""
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app import openclaw_control, reconciler
from app.activities import coding_activities
from app.activities import openclaw_activities as oa
from app.api import server
from app.coding import direct, runner
from app.db.models import Event
from tests.test_invariants import seed, session, task  # noqa: F401  (session is a fixture)

FAKES = Path(__file__).resolve().parent / "fakes" / "coding"
TASK = "11111111-2222-4333-8444-555555555555"
OTHER = "66666666-7777-4888-9999-000000000000"
CFG = {"model": "opus", "fallback_model": "sonnet", "max_turns": 50, "memory_max": "1G", "cpu_quota": "100%",
       "tasks_max": 64, "direct_turn_timeout_sec": 60, "direct_max_running": 2,
       "repositories": {"rmp": {"remote": "Hyper-AI-Lab/openclaw-jev", "source": "/root/.openclaw/rmp"}}}


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    units = tmp_path / "units"
    monkeypatch.setenv("PATH", f"{FAKES}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_UNITS_DIR", str(units))
    monkeypatch.setenv("FAKE_CLAUDE_ARGV", str(tmp_path / "argv.json"))
    monkeypatch.setattr(direct, "CLAUDE_BIN", FAKES / "claude")
    monkeypatch.setattr(direct, "DIRECT_DIR", tmp_path / "direct")
    monkeypatch.setattr(direct, "CONFIG_DIR", tmp_path / "claude-config")
    yield SimpleNamespace(tmp=tmp_path, argv=tmp_path / "argv.json", units=units)
    for pid_file in units.glob("*.pid"):
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def finished(session_id, number, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = direct.status(session_id, number)
        if state["done"]:
            return state
        time.sleep(0.05)
    raise AssertionError(f"turn {number} did not finish")


def flags(argv):
    return {argv[i]: argv[i + 1] for i in range(len(argv) - 1)
            if argv[i].startswith("--") and not argv[i + 1].startswith("--")}


def unit_name(session, number):
    return f"aura-direct-{session['task_id']}-{session['id'][:8]}-{number}"


def rewrite(session_id, **fields):
    path = direct._session_file(session_id)
    path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))


def test_only_auras_task_sessions_name_a_task():
    assert direct.task_of(f"agent:main:rmp_task_{TASK}") == TASK
    assert direct.task_of(f"agent:main:rmp_task_{TASK}__r2") == TASK
    for key in ("agent:main:main", f"agent:main:rmp_verify_{TASK}", f"agent:other:rmp_task_{TASK}", ""):
        assert direct.task_of(key) is None


def test_a_turn_runs_as_root_in_auto_mode_and_the_next_one_resumes_the_session(fakes):
    s = direct.create(TASK, "scratch", "Look at the logs", CFG)
    assert Path(s["path"]).is_dir() and s["status"] == "open" and s["title"] == "Look at the logs"
    assert direct.send(s["id"], "fixture:success_readonly what does the README say?", CFG) == {"session": s["id"], "turn": 1}
    first = finished(s["id"], 1)
    assert first["outcome"] == "success" and "HERON-7" in first["reply"]
    argv = json.loads(fakes.argv.read_text())
    assert flags(argv)["--permission-mode"] == "auto" and flags(argv)["--session-id"] == s["id"]
    assert "--resume" not in argv and "--permission-prompts" not in argv
    told = flags(argv)["--append-system-prompt"]
    assert s["path"] in told and "Never edit /root/.openclaw/rmp" in told and "Never push to main" in told
    unit = json.loads((fakes.units / f"{unit_name(s, 1)}.json").read_text())
    assert unit["env"]["HOME"] == "/root" and unit["env"]["CLAUDE_CONFIG_DIR"] == str(direct.CONFIG_DIR)
    assert unit["workdir"] == s["path"] and unit["props"]["RuntimeMaxSec"] == "60"

    # Claude Code keeps the conversation in its config directory once the session has started.
    transcript = direct.CONFIG_DIR / "projects" / "-scratch" / f"{s['id']}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    direct.send(s["id"], "fixture:resumed and the phrase?", CFG)
    second = finished(s["id"], 2)
    argv = json.loads(fakes.argv.read_text())
    assert second["reply"] == "OSPREY-4" and flags(argv)["--resume"] == s["id"] and "--session-id" not in argv
    assert direct.load(s["id"])["turns"] == 2


def test_a_session_takes_one_turn_at_a_time_and_the_host_a_few(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "0.5")
    a, b = direct.create(TASK, "scratch", "", CFG), direct.create(TASK, "scratch", "", CFG)
    direct.send(a["id"], "fixture:edit_and_test fix it", CFG)
    with pytest.raises(direct.SessionError, match="still running"):
        direct.send(a["id"], "fixture:success_readonly", CFG)
    with pytest.raises(direct.SessionError, match="too many"):
        direct.send(b["id"], "fixture:success_readonly", {**CFG, "direct_max_running": 1})
    assert direct.task_turn_running(TASK) and not direct.task_turn_running(OTHER)
    direct.end(a["id"])


def test_ending_a_session_stops_its_turn_and_closes_it(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "0.5")
    s = direct.create(TASK, "scratch", "", CFG)
    direct.send(s["id"], "fixture:edit_and_test fix it", CFG)
    ended = direct.end(s["id"], "ended by Aura")
    assert ended["status"] == "ended" and ended["end_reason"] == "ended by Aura" and ended["stopped"] == [unit_name(s, 1)]
    assert finished(s["id"], 1)["outcome"] == "stopped"
    with pytest.raises(direct.SessionError, match="has ended"):
        direct.send(s["id"], "fixture:success_readonly", CFG)


def test_a_stop_ends_every_claude_session_of_the_task_and_no_other(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DELAY", "0.5")
    monkeypatch.setattr(coding_activities, "RUNS_DIR", fakes.tmp / "runs")
    a, b = direct.create(TASK, "scratch", "", CFG), direct.create(TASK, "scratch", "", CFG)
    other = direct.create(OTHER, "scratch", "", CFG)
    direct.send(a["id"], "fixture:edit_and_test fix it", CFG)
    assert coding_activities.stop_task_units(TASK) == [unit_name(a, 1)]
    assert {s["id"]: s["status"] for s in direct.sessions()} == {a["id"]: "ended", b["id"]: "ended", other["id"]: "open"}


async def test_aborting_auras_runs_ends_her_claude_sessions_first(fakes, monkeypatch):
    s = direct.create(TASK, "scratch", "", CFG)
    monkeypatch.setattr(openclaw_control, "task_run_session_keys", lambda task_id: [])
    assert await openclaw_control.abort_task_runs(TASK, reason="stop") == []
    assert direct.load(s["id"])["status"] == "ended" and direct.load(s["id"])["end_reason"] == "stop"


def test_a_finished_turn_books_its_tokens_once(fakes, monkeypatch):
    booked = []
    monkeypatch.setattr("app.llm.usage_monitor.record_request", lambda *args, **kw: booked.append(args))
    s = direct.create(TASK, "scratch", "", CFG)
    direct.send(s["id"], "fixture:success_readonly", CFG)
    finished(s["id"], 1)
    direct.status(s["id"], 1)
    assert [args[0] for args in booked] == [runner.USAGE_PROFILE]


def test_a_turn_whose_unit_vanished_without_an_exit_line_is_done_with_no_result(fakes):
    s = direct.create(TASK, "scratch", "", CFG)
    turn = direct._turn(s, 1)
    turn.dir.mkdir(parents=True)
    turn.stream_file.write_text('{"type":"system","subtype":"init","session_id":"x"}\n')
    rewrite(s["id"], turns=1)
    state = direct.status(s["id"], 1)
    assert state["done"] and state["outcome"] == "no_result"


def test_empty_or_oversized_messages_unknown_workspaces_and_odd_ids_are_refused(fakes):
    with pytest.raises(direct.SessionError, match="workspace"):
        direct.create(TASK, "live", "", CFG)
    s = direct.create(TASK, "scratch", "", CFG)
    for message, words in (("  ", "empty"), ("x" * (direct.MAX_MESSAGE_BYTES + 1), "bytes")):
        with pytest.raises(direct.SessionError, match=words):
            direct.send(s["id"], message, CFG)
    for odd in ("../../etc", "*", ""):
        with pytest.raises(direct.SessionError, match="no session"):
            direct.load(odd)
    assert direct.sessions("*") == []


def test_a_repo_workspace_is_cloned_from_github_with_the_live_checkout_lending_its_objects(fakes, monkeypatch):
    calls = []

    def git(*args, timeout=60):
        calls.append(args)
        if args[0] == "clone":
            Path(args[-1]).mkdir()

    monkeypatch.setattr(direct, "_git", git)
    s = direct.create(TASK, "repo", "", CFG)
    assert s["path"].endswith("/repo") and calls[0] == (
        "clone", "--quiet", "--reference-if-able", "/root/.openclaw/rmp", "--dissociate",
        "https://github.com/Hyper-AI-Lab/openclaw-jev.git", s["path"])
    assert ("-C", s["path"], "config", "user.name", "Aura (Claude Code)") in calls

    def broken(*args, timeout=60):
        raise subprocess.CalledProcessError(128, "git", stderr="fatal: unable to access")

    monkeypatch.setattr(direct, "_git", broken)
    with pytest.raises(subprocess.CalledProcessError):
        direct.create(TASK, "repo", "", CFG)
    assert [x["id"] for x in direct.sessions(TASK)] == [s["id"]]


def test_the_clone_is_real_git_that_borrows_objects_and_then_stands_alone(tmp_path):
    def git(*args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    origin, reference = tmp_path / "origin.git", tmp_path / "live"
    git("init", "--quiet", "--bare", "-b", "main", str(origin))
    git("clone", "--quiet", str(origin), str(reference))
    (reference / "README.md").write_text("hello\n")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "--allow-empty", "-m", "first", cwd=reference)
    git("add", "README.md", cwd=reference)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "readme", cwd=reference)
    git("push", "--quiet", "origin", "main", cwd=reference)
    direct._clone(tmp_path / "work", origin.as_uri(), str(reference))
    assert (tmp_path / "work" / "README.md").read_text() == "hello\n"
    assert not (tmp_path / "work" / ".git" / "objects" / "info" / "alternates").exists()
    assert subprocess.run(["git", "-C", str(tmp_path / "work"), "config", "user.name"],
                          capture_output=True, text=True).stdout.strip() == "Aura (Claude Code)"


def test_prune_removes_only_the_workspaces_of_sessions_that_ended_long_ago(fakes):
    old, recent, still_open = (direct.create(TASK, "scratch", "", CFG) for _ in range(3))
    direct.end(old["id"])
    direct.end(recent["id"])
    rewrite(old["id"], ended_at="2026-01-01T00:00:00+00:00")
    assert direct.prune(14) == [old["id"]]
    assert not Path(old["path"]).exists() and Path(recent["path"]).exists() and Path(still_open["path"]).exists()
    assert direct.load(old["id"])["status"] == "ended"


def test_auras_reply_deadline_moves_on_only_while_her_claude_turn_works(monkeypatch):
    running = {"now": True}
    monkeypatch.setattr(direct, "task_turn_running", lambda task_id: running["now"])
    soon = time.time() + 10
    assert oa._reply_deadline(soon, TASK, None) >= time.time() + 119
    assert oa._reply_deadline(soon, TASK, soon) == soon
    assert oa._reply_deadline(soon, None, None) == soon
    running["now"] = False
    assert oa._reply_deadline(soon, TASK, None) == soon


async def test_the_reconciler_ends_sessions_whose_task_has_finished(fakes, session):  # noqa: F811
    await seed(session, task(TASK, status="completed"), task(OTHER, status="running"))
    finished_task, live_task = direct.create(TASK, "scratch", "", CFG), direct.create(OTHER, "scratch", "", CFG)
    async with session() as db:
        assert await reconciler._end_claude_sessions_of_finished_tasks(db) == 1
    assert direct.load(finished_task["id"])["end_reason"] == "task finished"
    assert direct.load(live_task["id"])["status"] == "open"


async def test_the_api_runs_a_session_for_a_live_task_and_records_it(fakes, session, monkeypatch):  # noqa: F811
    await seed(session, task(TASK, status="running"), task(OTHER, status="completed"))
    monkeypatch.setattr("app.config.get_coding_config", lambda: CFG)

    async def db():
        async with session() as s:
            yield s

    server.app.dependency_overrides[server.get_db] = db
    monkeypatch.setenv("RMP_API_KEY", "k")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://rmp",
                                     headers={"X-RMP-API-Key": "k"}) as api:
            refused = await api.post("/api/claude/sessions", json={"session_key": f"agent:main:rmp_task_{OTHER}"})
            created = (await api.post("/api/claude/sessions", json={
                "session_key": f"agent:main:rmp_task_{TASK}", "workspace": "scratch", "title": "Logs"})).json()
            sent = (await api.post(f"/api/claude/sessions/{created['id']}/messages",
                                   json={"message": "fixture:success_readonly hello"})).json()
            turn = (await api.get(f"/api/claude/sessions/{created['id']}/turns/1", params={"wait": 20})).json()
            listed = (await api.get("/api/claude/sessions", params={"task_id": TASK})).json()
            ended = (await api.post(f"/api/claude/sessions/{created['id']}/end")).json()
            after = await api.post(f"/api/claude/sessions/{created['id']}/messages", json={"message": "again"})
            missing = await api.get("/api/claude/sessions/nope/turns/1")
    finally:
        server.app.dependency_overrides.pop(server.get_db, None)
    assert refused.status_code == 409
    assert sent == {"session": created["id"], "turn": 1} and turn["done"] and turn["outcome"] == "success"
    assert [s["id"] for s in listed["sessions"]] == [created["id"]] and ended["status"] == "ended"
    assert after.status_code == 409 and missing.status_code == 404
    async with session() as s:
        events = (await s.execute(select(Event).where(Event.entity_id == TASK))).scalars().all()
    assert sorted(e.event_type for e in events) == ["claude.session_ended", "claude.session_started", "claude.turn_started"]
