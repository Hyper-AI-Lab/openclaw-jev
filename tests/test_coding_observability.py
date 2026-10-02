"""Coding readiness checks, the deploy and unit invariants, and the coding API."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.api import server
from app.db.models import Event
from app.production import coding_readiness as cr, invariants
from tests.test_invariants import ago, seed, session, task  # noqa: F401  (session is a fixture)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A Claude Code install, token, smoke record and managed settings, all healthy."""
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "2.1.280").write_text("#!/bin/sh\n")
    (versions / "2.1.280").chmod(0o755)
    (tmp_path / "claude").symlink_to(versions / "2.1.280")
    (tmp_path / "claude.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=x\n")
    meta = tmp_path / "claude-token.json"
    smoke = tmp_path / "claude_smoke.json"
    settings, source = tmp_path / "managed.json", tmp_path / "source.json"
    settings.write_text('{"a": 1}')
    source.write_text('{"a": 1}')

    def token(days):
        meta.write_text(json.dumps({"expires_at": (datetime.now(timezone.utc) + timedelta(days=days, hours=1)).isoformat(),
                                    "fingerprint": "abc"}))

    token(300)
    smoke.write_text(json.dumps({"at": "2026-09-30T15:57:30+00:00", "ok": True}))
    for name, value in (("CLAUDE_BIN", tmp_path / "claude"), ("TOKEN_ENV_FILE", tmp_path / "claude.env"),
                        ("TOKEN_META_FILE", meta), ("SMOKE_RECORD", smoke), ("MANAGED_SETTINGS", settings),
                        ("MANAGED_SETTINGS_SOURCE", source), ("JOBS_DIR", tmp_path / "jobs"), ("RUNS_DIR", tmp_path / "runs")):
        monkeypatch.setattr(cr, name, value)
    monkeypatch.setattr(cr, "get_coding_config", lambda: {"claude_version": "2.1.280", "run_timeout_sec": 5400,
                                                          "blocked_tcp_ports": [22], "enabled": True, "repositories": {}})
    monkeypatch.setattr(cr.firewall, "active", lambda: True)
    monkeypatch.setattr(cr.firewall, "uncovered_listeners", lambda ports: [])
    return SimpleNamespace(tmp=tmp_path, token=token, smoke=smoke, settings=settings, versions=versions)


def test_claude_code_is_ready_with_the_pinned_version_a_valid_token_and_a_passing_smoke_run(host):
    result = cr.check_claude_code()
    assert result.status == "pass" and result.details["version"] == "2.1.280" and result.details["token_days_left"] == 300


@pytest.mark.parametrize("break_it, status, words", [
    (lambda h: h.token(10), "warn", "expires in 10 days"),
    (lambda h: h.token(-2), "fail", "has expired"),
    (lambda h: h.smoke.unlink(), "warn", "no smoke run recorded"),
    (lambda h: h.smoke.write_text('{"ok": false}'), "warn", "last smoke run failed"),
    (lambda h: ((h.versions / "2.1.300").write_text(""), (h.tmp / "claude").unlink(),
                (h.tmp / "claude").symlink_to(h.versions / "2.1.300")), "fail", "pinned 2.1.280"),
])
def test_claude_code_readiness_names_what_is_wrong(host, break_it, status, words):
    break_it(host)
    result = cr.check_claude_code()
    assert result.status == status and words in result.message


def test_isolation_fails_on_a_missing_firewall_an_open_listener_or_changed_managed_settings(host, monkeypatch):
    assert cr.check_coding_isolation().status == "pass"
    host.settings.write_text('{"a": 2}')
    monkeypatch.setattr(cr.firewall, "active", lambda: False)
    monkeypatch.setattr(cr.firewall, "uncovered_listeners", lambda ports: ["127.0.0.1:9999"])
    result = cr.check_coding_isolation()
    assert result.status == "fail"
    for words in ("firewall is not loaded", "127.0.0.1:9999", "differs from the repo's copy"):
        assert words in result.message


def test_coding_jobs_warns_about_a_stuck_unit_and_a_checkout_without_a_job(host, monkeypatch):
    assert cr.check_coding_jobs().status == "pass"
    (host.tmp / "jobs" / "lost-task").mkdir(parents=True)
    monkeypatch.setattr(cr, "_uptime_usec", lambda: 10_000 * 1_000_000)
    monkeypatch.setattr(cr, "live_units", lambda: [{"unit": "aura-claude-t-1", "kind": "claude", "task_id": "t",
                                                    "active_usec": 1_000_000}])
    result = cr.check_coding_jobs()
    assert result.status == "warn" and "aura-claude-t-1" in result.message and "lost-task" in result.message


def deploy_event(tid, status, minutes, **payload):
    return Event(correlation_id=tid, entity_type="task", entity_id=tid, event_type="coding.deploy",
                 event_payload={"status": status, **payload}, occurred_at=ago(minutes=minutes))


def approval(tid, minutes):
    return Event(correlation_id=tid, entity_type="task", entity_id=tid, event_type="approval.confirmed",
                 event_payload={"ok": True}, occurred_at=ago(minutes=minutes))


async def test_a_shipped_change_needs_an_approval_confirmed_before_it(session):  # noqa: F811
    await seed(session, approval("ok", 30), deploy_event("ok", "deployed", 20),
               deploy_event("blocked", "blocked", 20), deploy_event("pr", "pr_opened", 20), approval("late", 5),
               deploy_event("late", "rolled_back", 10))
    result = await invariants.check_approved_deploys()
    assert result.status == "fail" and result.details["task_ids"] == ["late", "pr"]
    await seed(session, approval("pr", 25))
    async with session() as db:
        await db.execute(Event.__table__.delete().where(Event.entity_id == "late"))
        await db.commit()
    assert (await invariants.check_approved_deploys()).status == "pass"


async def test_every_self_deploy_records_its_suite_and_checks(session):  # noqa: F811
    await seed(session, deploy_event("good", "deployed", 5, suite={"ok": True}, canary="ok"),
               deploy_event("bare", "deployed", 5), deploy_event("pr", "pr_opened", 5))
    result = await invariants.check_deploy_verification()
    assert result.status == "fail" and result.details["task_ids"] == ["bare"]


async def test_no_claude_code_unit_runs_without_a_live_task(session, monkeypatch):  # noqa: F811
    await seed(session, task("live", status="running"), task("done", status="completed"))
    units = [{"unit": f"aura-claude-{tid}-1", "kind": "claude", "task_id": tid, "active_usec": 1} for tid in ("live", "done")]
    monkeypatch.setattr(cr, "live_units", lambda: units)
    result = await invariants.check_coding_units()
    assert result.status == "fail" and result.details["units"] == ["aura-claude-done-1"]
    monkeypatch.setattr(cr, "live_units", lambda: units[:1])
    assert (await invariants.check_coding_units()).status == "pass"


async def test_the_coding_api_shows_a_job_and_refuses_a_path_outside_the_runs_directory(host, monkeypatch):
    root = host.tmp / "runs" / "t1"
    (root / "1").mkdir(parents=True)
    (root / "job.json").write_text(json.dumps({"task_id": "t1", "repo": "rmp", "branch": "aura/t1-x"}))
    (root / "1" / "meta.json").write_text(json.dumps({"unit": "aura-claude-t1-1", "model": "opus"}))
    (root / "1" / "exit").write_text("success exited 0\n")
    (root / "verify-1").mkdir()
    (root / "verify-1" / "result.json").write_text(json.dumps({"ok": True, "commands": [{"exit": "success exited 0",
                                                                                         "tail": "long output"}]}))
    monkeypatch.setattr(cr.runner, "unit_active", lambda unit: False)

    class NoEvents:
        async def execute(self, query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    async def db():
        yield NoEvents()

    server.app.dependency_overrides[server.get_db] = db
    monkeypatch.setenv("RMP_API_KEY", "k")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://rmp",
                                     headers={"X-RMP-API-Key": "k"}) as api:
            job = (await api.get("/api/coding/jobs/t1")).json()
            outside = await api.get("/api/coding/jobs/..%2F..%2Fetc")
            missing = await api.get("/api/coding/jobs/nope")
    finally:
        server.app.dependency_overrides.pop(server.get_db, None)
    assert job["job"]["branch"] == "aura/t1-x" and job["runs"][0]["exit"] == "success exited 0"
    assert job["runs"][0]["outcome"] == "no_result" and job["verifications"] == [{"attempt": "1", "ok": True,
                                                                                  "commands": [{"exit": "success exited 0"}]}]
    assert outside.status_code == 404 and missing.status_code == 404


async def test_the_coding_status_names_the_short_rmp_commit_alongside_the_existing_fields(host, monkeypatch):
    import subprocess
    from pathlib import Path

    from app.activities import coding_activities

    monkeypatch.setattr(cr, "live_units", lambda: [])
    monkeypatch.setattr(coding_activities, "slot_holder", lambda: None)
    monkeypatch.setenv("RMP_API_KEY", "k")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://rmp",
                                 headers={"X-RMP-API-Key": "k"}) as api:
        response = await api.get("/api/coding/status")
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(server.__file__).parent,
                          capture_output=True, text=True, check=True).stdout.strip()
    status = response.json()
    assert response.status_code == 200 and status["rmp_commit"] == head and len(head) >= 7
    assert {"enabled", "claude_version", "pinned", "token_days_left", "slot_holder", "live_units", "repositories",
            "jobs"} <= status.keys()
    assert status["enabled"] is True and status["pinned"] == "2.1.280" and status["repositories"] == {}
