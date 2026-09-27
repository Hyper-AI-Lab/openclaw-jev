"""Tests for canary sentinel and ops notification helpers."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

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
