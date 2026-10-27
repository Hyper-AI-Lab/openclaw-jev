"""Tests for canary sentinel and ops notification helpers."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.notification_policy import is_internal_task, is_smoke_test_intent
from app.production.canary_sentinel import (
    evaluate_health_canary,
    evaluate_memory_canary,
    write_health_canary_result,
)


def test_intake_smoke_is_internal():
    intent = "Intake attach smoke 20260608: reply CANARY_OK"
    assert is_smoke_test_intent(intent)
    assert is_internal_task(intent, "user", ["intake-smoke"]) is True
    assert is_internal_task(intent, "canary", ["canary", "system"]) is True


def test_evaluate_health_canary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.production.canary_sentinel.HEALTH_CANARY_PATH",
        tmp_path / "missing.json",
    )
    issue = evaluate_health_canary()
    assert issue is not None
    assert issue.name == "health_canary"
    assert issue.status == "missing"


def test_reap_stale_llm_slots_releases_terminal_task(monkeypatch, tmp_path):
    from app.llm import quota_broker

    state_file = tmp_path / "quota.json"
    monkeypatch.setattr(quota_broker, "STATE_PATH", state_file)
    task_id = "fa6abf5a-35b7-4782-9912-5ae440e6f020"
    session = f"agent:main:rmp_task_{task_id}"
    quota_broker._write_state_unlocked(
        {
            "keys": {"nvidia:default": {"in_flight": 1}},
            "global": {
                "active_slots": {
                    "slot-1": {
                        "profile_id": "nvidia:default",
                        "started_ms": quota_broker._now_ms(),
                        "session_key": session,
                    }
                },
                "session_slots": {session: "slot-1"},
            },
        }
    )
    monkeypatch.setattr(quota_broker, "_lookup_task_status_sync", lambda tid: "failed")
    reaped = quota_broker.reap_stale_llm_slots_sync(max_age_ms=999_999_999)
    assert reaped
    assert quota_broker.get_orchestration_status()["active_slots"] == 0


def test_evaluate_health_canary_ok(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    monkeypatch.setattr("app.production.canary_sentinel.HEALTH_CANARY_PATH", path)
    write_health_canary_result(status="completed", task_id="abc")
    assert evaluate_health_canary() is None


def test_evaluate_health_canary_failed(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    monkeypatch.setattr("app.production.canary_sentinel.HEALTH_CANARY_PATH", path)
    write_health_canary_result(status="failed", task_id="abc", error="timeout")
    issue = evaluate_health_canary()
    assert issue is not None
    assert issue.status == "failed"


def test_evaluate_memory_canary_stale(tmp_path, monkeypatch):
    import json

    path = tmp_path / "memory.json"
    monkeypatch.setattr("app.production.canary_sentinel.MEMORY_CANARY_PATH", path)
    old = (datetime.utcnow() - timedelta(hours=30)).isoformat() + "Z"
    path.write_text(
        json.dumps({"status": "completed", "finished_at": old, "search_bad": 0, "memory_ok": 1, "prompt_ok": 1})
    )
    issue = evaluate_memory_canary()
    assert issue is not None
    assert issue.status == "stale"


def test_evaluate_memory_timeout_with_transcript_ok_is_not_an_issue(tmp_path, monkeypatch):
    import json

    path = tmp_path / "memory.json"
    monkeypatch.setattr("app.production.canary_sentinel.MEMORY_CANARY_PATH", path)
    path.write_text(
        json.dumps(
            {
                "status": "timeout",
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "memory_ok": 1,
                "prompt_ok": 1,
                "search_bad": 0,
            }
        )
    )
    assert evaluate_memory_canary() is None


def test_evaluate_memory_inconclusive_is_issue(tmp_path, monkeypatch):
    import json

    path = tmp_path / "memory.json"
    monkeypatch.setattr("app.production.canary_sentinel.MEMORY_CANARY_PATH", path)
    path.write_text(
        json.dumps(
            {
                "status": "inconclusive",
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "memory_ok": 0,
                "prompt_ok": 0,
            }
        )
    )
    issue = evaluate_memory_canary()
    assert issue is not None
    assert issue.status == "inconclusive"


def test_evaluate_memory_completed_unproven_is_issue(tmp_path, monkeypatch):
    import json

    path = tmp_path / "memory.json"
    monkeypatch.setattr("app.production.canary_sentinel.MEMORY_CANARY_PATH", path)
    path.write_text(
        json.dumps(
            {
                "status": "completed",
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "memory_ok": 0,
                "prompt_ok": 0,
                "search_bad": 0,
            }
        )
    )
    issue = evaluate_memory_canary()
    assert issue is not None
    assert issue.status == "memory_unproven"


def test_inspect_memory_canary_uses_sqlite(monkeypatch, tmp_path):
    from app.production.canary_sentinel import inspect_memory_canary_transcript
    from tests.test_openclaw_sessions import _make_agent_db
    from app import openclaw_sessions as osess

    db = _make_agent_db(tmp_path)
    monkeypatch.setattr(osess, "AGENT_DB_PATH", db)
    monkeypatch.setattr(osess, "SESSIONS_JSON_PATH", tmp_path / "missing-sessions.json")
    monkeypatch.setattr(osess, "SESSIONS_DIR", tmp_path)
    out = inspect_memory_canary_transcript("not-the-intake-id")
    assert out["has_transcript"] is False
    # reuse intake session key shape by inspecting via get_session_entry
    from app.openclaw_sessions import get_session_entry, read_transcript_lines

    entry = get_session_entry("agent:main:rmp_intake_x")
    lines = read_transcript_lines(entry["sessionId"])
    assert "hello" in lines[0]


def test_memory_canary_readiness_warns_unproven(monkeypatch, tmp_path):
    from app.production import readiness as rd

    path = tmp_path / "last_memory_canary.json"
    path.write_text(
        '{"status":"completed","finished_at":"2099-01-01T00:00:00Z","memory_ok":0,"prompt_ok":0}'
    )
    monkeypatch.setattr(rd, "MEMORY_CANARY_RESULT_PATH", str(path))
    r = rd.check_memory_canary_recency()
    assert r.status == "warn"


def test_evaluate_health_deferred_recent_is_ok(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    monkeypatch.setattr("app.production.canary_sentinel.HEALTH_CANARY_PATH", path)
    write_health_canary_result(status="deferred", error="active_user_tasks")
    assert evaluate_health_canary() is None


@pytest.mark.asyncio
async def test_run_sentinel_alerts_on_failure(tmp_path, monkeypatch):
    from app.production import canary_sentinel

    health_path = tmp_path / "health.json"
    monkeypatch.setattr(canary_sentinel, "HEALTH_CANARY_PATH", health_path)
    monkeypatch.setattr(canary_sentinel, "MEMORY_CANARY_PATH", tmp_path / "mem.json")
    monkeypatch.setattr(canary_sentinel, "ALERT_STATE_PATH", tmp_path / "alerts.json")
    monkeypatch.setattr(canary_sentinel, "evaluate_runtime_code_sync", lambda: None)
    monkeypatch.setattr(canary_sentinel, "evaluate_llm_usage", lambda: None)
    monkeypatch.setattr("app.production.invariants.run_invariant_checks", AsyncMock(return_value=[]))
    canary_sentinel.write_health_canary_result(status="failed", task_id="x", error="boom")

    with patch.object(canary_sentinel, "attempt_remediation", return_value=[]):
        with patch.object(canary_sentinel, "_wait_for_llm_capacity", return_value=None):
            with patch.object(canary_sentinel, "count_active_user_tasks_sync", return_value=0):
                with patch.object(canary_sentinel, "rerun_health_canary_sync", return_value=False):
                    with patch.object(
                        canary_sentinel, "notify_ops_slack", new_callable=AsyncMock, return_value=True
                    ) as notify:
                        with patch.object(
                            canary_sentinel, "send_alert", new_callable=AsyncMock, return_value=True
                        ):
                            result = await canary_sentinel.run_sentinel(trigger="test")
    assert result["issues"]
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_sentinel_recovery_rerun_clears_timeout_without_alert(tmp_path, monkeypatch):
    import json

    from app.production import canary_sentinel

    health_path = tmp_path / "health.json"
    mem_path = tmp_path / "mem.json"
    monkeypatch.setattr(canary_sentinel, "HEALTH_CANARY_PATH", health_path)
    monkeypatch.setattr(canary_sentinel, "MEMORY_CANARY_PATH", mem_path)
    monkeypatch.setattr(canary_sentinel, "ALERT_STATE_PATH", tmp_path / "alerts.json")
    monkeypatch.setattr(canary_sentinel, "evaluate_runtime_code_sync", lambda: None)
    monkeypatch.setattr(canary_sentinel, "evaluate_llm_usage", lambda: None)
    monkeypatch.setattr("app.production.invariants.run_invariant_checks", AsyncMock(return_value=[]))
    canary_sentinel.write_health_canary_result(status="timeout", task_id="stuck", error="poll timeout")
    mem_path.write_text(
        json.dumps(
            {
                "status": "timeout",
                "finished_at": datetime.utcnow().isoformat() + "Z",
                "memory_ok": 1,
                "prompt_ok": 1,
                "search_bad": 0,
            }
        )
    )

    def _prove():
        canary_sentinel.write_health_canary_result(status="completed", task_id="recovered")
        return True

    with patch.object(canary_sentinel, "attempt_remediation", return_value=["cancelled canary task stuck"]):
        with patch.object(canary_sentinel, "_wait_for_llm_capacity", return_value=None):
            with patch.object(canary_sentinel, "count_active_user_tasks_sync", return_value=0):
                with patch.object(canary_sentinel, "rerun_health_canary_sync", side_effect=_prove):
                    with patch.object(
                        canary_sentinel, "notify_ops_slack", new_callable=AsyncMock, return_value=True
                    ) as notify:
                        with patch.object(
                            canary_sentinel, "send_alert", new_callable=AsyncMock, return_value=True
                        ) as alert:
                            result = await canary_sentinel.run_sentinel(trigger="health_canary")
    assert result.get("resolved_by_remediation") is True
    assert result.get("recovery_canary_ok") is True
    notify.assert_not_awaited()
    alert.assert_not_awaited()


def _usage_report(hours, *, prompt=0, attempts=0, aborted=0, ctx=0):
    return {
        "available": True,
        "window_hours": hours,
        "totals": {
            "attempts": attempts,
            "aborted": aborted,
            "input_tokens": prompt,
            "cache_read_tokens": 0,
            "output_tokens": 0,
            "aborted_prompt_tokens": 0,
        },
        "abort_rate": (aborted / attempts) if attempts else 0.0,
        "max_live_context": {"session_key": "agent:main:rmp_task_x", "tokens": ctx},
    }


def test_evaluate_llm_usage_flags_an_ongoing_burn(monkeypatch):
    from app.llm import usage_monitor
    from app.production import canary_sentinel

    reports = {
        24: _usage_report(24, prompt=9_000_000, attempts=100, aborted=40),
        usage_monitor.RECENT_WINDOW_HOURS: _usage_report(
            usage_monitor.RECENT_WINDOW_HOURS, prompt=1_000_000, attempts=10, aborted=4
        ),
    }
    monkeypatch.setattr(usage_monitor, "transcript_usage", lambda hours=24, **_: reports[hours])
    monkeypatch.setattr(usage_monitor, "_input_budget_24h", lambda: 5_000_000)
    issue = canary_sentinel.evaluate_llm_usage()
    assert issue is not None and issue.name == "llm_usage"
    assert "over budget 5,000,000" in issue.message
    assert "abort rate 40%" in issue.message


def test_evaluate_llm_usage_ignores_a_burn_that_already_stopped(monkeypatch):
    from app.llm import usage_monitor
    from app.production import canary_sentinel

    reports = {
        24: _usage_report(24, prompt=9_000_000, attempts=100, aborted=40),
        usage_monitor.RECENT_WINDOW_HOURS: _usage_report(
            usage_monitor.RECENT_WINDOW_HOURS, prompt=50_000, attempts=10, aborted=0
        ),
    }
    monkeypatch.setattr(usage_monitor, "transcript_usage", lambda hours=24, **_: reports[hours])
    monkeypatch.setattr(usage_monitor, "_input_budget_24h", lambda: 5_000_000)
    assert canary_sentinel.evaluate_llm_usage() is None


def test_evaluate_llm_usage_flags_a_large_live_context(monkeypatch):
    from app.llm import usage_monitor
    from app.production import canary_sentinel

    recent = usage_monitor.RECENT_WINDOW_HOURS
    reports = {24: _usage_report(24, ctx=75_000), recent: _usage_report(recent)}
    monkeypatch.setattr(usage_monitor, "transcript_usage", lambda hours=24, **_: reports[hours])
    issue = canary_sentinel.evaluate_llm_usage()
    assert issue is not None
    assert "75,000 context tokens" in issue.message


def test_the_idle_gate_counts_exactly_idle_blocking(tmp_path, monkeypatch):
    """The deploy's idle gate counts user tasks in IDLE_BLOCKING: not deploying (the self-deploy's own task), not
    blocked (a queued coding job), not a finished task and not a canary."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from app.db import database
    from app.db.models import Base, Task
    from app.production import canary_sentinel
    from app.status import IDLE_BLOCKING, TaskStatus

    url = f"sqlite:///{tmp_path / 'idle.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine, tables=[Task.__table__])
    monkeypatch.setattr(database, "DATABASE_URL", url)

    def seed(*statuses):
        with Session(engine) as db:
            db.add_all([Task(id=f"user-{status}", goal=f"Plan the trip ({status})", task_type="user", status=status)
                        for status in statuses])
            db.commit()

    with Session(engine) as db:
        db.add(Task(id="canary", goal="RMP CANARY: Reply with exactly CANARY_OK on its own line. No tools.",
                    task_type="canary", status=TaskStatus.RUNNING))
        db.commit()
    seed(TaskStatus.DEPLOYING, TaskStatus.BLOCKED)
    assert canary_sentinel.count_active_user_tasks_sync(strict=True) == 0

    seed(*(status for status in TaskStatus if status not in (TaskStatus.DEPLOYING, TaskStatus.BLOCKED)))
    engine.dispose()
    assert IDLE_BLOCKING == {"created", "running", "pending_user_input"}
    assert canary_sentinel.count_active_user_tasks_sync() == len(IDLE_BLOCKING)
    assert canary_sentinel.count_active_user_tasks_sync(strict=True) == len(IDLE_BLOCKING)

    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{tmp_path / 'missing' / 'idle.db'}")
    with pytest.raises(OperationalError):
        canary_sentinel.count_active_user_tasks_sync(strict=True)
    assert canary_sentinel.count_active_user_tasks_sync() == 0


async def test_remediation_cancels_a_stuck_canary_inside_the_running_loop(tmp_path, monkeypatch):
    """R-128 (4b): the sentinel runs in an event loop. Its remediation awaits the cancel of the canary that the soft
    health issue names; that cancel terminates the canary's workflow, and no coroutine is left un-awaited (the sync
    cancel's asyncio.run raised inside the running loop and dropped its terminate)."""
    import gc
    import json
    import warnings

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app import temporal_control
    from app.db.models import Base, Event, ProcessRun, Task
    from app.production import canary_sentinel

    tid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rmp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Task.__table__, ProcessRun.__table__, Event.__table__])
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(Task(id=tid, goal="RMP CANARY: Reply with exactly CANARY_OK", task_type="canary", status="running"))
        await db.commit()
    client = MagicMock()
    handle = client.get_workflow_handle.return_value
    handle.signal, handle.terminate = AsyncMock(), AsyncMock()
    abort = AsyncMock(return_value=[])
    cancel = AsyncMock(wraps=temporal_control.cancel_task)
    for module in (temporal_control, canary_sentinel):
        monkeypatch.setattr(module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(temporal_control, "connect_temporal", AsyncMock(return_value=client))
    monkeypatch.setattr("app.openclaw_control.abort_task_runs", abort)
    monkeypatch.setattr("app.task_registry.hooks.index_terminal_task_async", AsyncMock())
    monkeypatch.setattr(canary_sentinel, "cancel_task", cancel)

    health_path, mem_path = tmp_path / "health.json", tmp_path / "mem.json"
    monkeypatch.setattr(canary_sentinel, "HEALTH_CANARY_PATH", health_path)
    monkeypatch.setattr(canary_sentinel, "MEMORY_CANARY_PATH", mem_path)
    monkeypatch.setattr(canary_sentinel, "ALERT_STATE_PATH", tmp_path / "alerts.json")
    monkeypatch.setattr(canary_sentinel, "evaluate_runtime_code_sync", lambda: None)
    monkeypatch.setattr(canary_sentinel, "evaluate_llm_usage", lambda: None)
    monkeypatch.setattr(canary_sentinel, "reap_stale_llm_slots_sync", lambda: [])
    monkeypatch.setattr(canary_sentinel, "_systemctl_is_active", lambda unit: True)
    monkeypatch.setattr(canary_sentinel, "count_active_user_tasks_sync", lambda: 0)
    monkeypatch.setattr(canary_sentinel, "_wait_for_llm_capacity", lambda: None)
    monkeypatch.setattr("app.production.invariants.run_invariant_checks", AsyncMock(return_value=[]))
    canary_sentinel.write_health_canary_result(status="timeout", task_id=tid, error="poll timeout")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    mem_path.write_text(json.dumps({"status": "completed", "finished_at": now.isoformat() + "Z",
                                    "memory_ok": 1, "prompt_ok": 1, "search_bad": 0}))

    def prove():
        canary_sentinel.write_health_canary_result(status="completed", task_id="recovered")
        return True

    monkeypatch.setattr(canary_sentinel, "rerun_health_canary_sync", prove)
    # Garbage of earlier tests is collected first, so only this run's coroutines can warn below.
    gc.collect()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = await canary_sentinel.run_sentinel(trigger="health_canary")
            gc.collect()
        async with factory() as db:
            task = await db.get(Task, tid)
    finally:
        await engine.dispose()

    cancel.assert_awaited_once_with(tid, reason="health_canary_timeout", actor="canary_sentinel", wait_abort=True)
    abort.assert_awaited_once_with(tid, reason="health_canary_timeout")
    client.get_workflow_handle.assert_called_once_with(f"workflow-{tid}")
    handle.terminate.assert_awaited_once_with("health_canary_timeout")
    assert task.status == "cancelled"
    assert f"cancelled canary task {tid[:8]}" in result["remediation"] and result["resolved_by_remediation"]
    assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []


@pytest.mark.parametrize("result, code", [
    ({"issues": [], "invariants": {"broken": []}}, 0),
    ({"issues": [{"name": "health_canary", "status": "timeout"}], "invariants": {"broken": []}}, 1),
], ids=["healthy", "issues"])
def test_main_flushes_then_leaves_through_os_exit(monkeypatch, result, code):
    """A remediation cancel leaves the sentinel holding a Temporal client, whose native threads can abort interpreter
    finalization (exit 134). main prints the verdict, flushes logging and its output, and leaves through os._exit with
    the exit code it always had, as ops/temporal_healthcheck.py does."""
    import io
    import json
    import sys

    from app.production import canary_sentinel

    steps = []

    class Exited(Exception):
        """os._exit never returns."""

    async def run_sentinel(*, trigger):
        steps.append(("run", trigger))
        return result

    def leave(status):
        steps.append(("exit", status))
        raise Exited

    class Output(io.StringIO):
        def flush(self):
            steps.append(("output flushed",))

    out = Output()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "argv", ["canary_sentinel", "--trigger", "health_canary"])
    monkeypatch.setattr(canary_sentinel, "run_sentinel", run_sentinel)
    monkeypatch.setattr(canary_sentinel.logging, "shutdown", lambda: steps.append(("logging flushed",)))
    monkeypatch.setattr(canary_sentinel.os, "_exit", leave)

    with pytest.raises(Exited):
        canary_sentinel.main()

    assert steps == [("run", "health_canary"), ("logging flushed",), ("output flushed",), ("exit", code)]
    assert json.loads(out.getvalue()) == result
