"""Tests for runtime code sync detection."""
import json
import os
import time
from pathlib import Path

from app.production import runtime_sync


def test_mark_and_detect_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_sync, "BOOT_DIR", tmp_path)
    monkeypatch.setattr(runtime_sync, "RMP_ROOT", tmp_path)
    monkeypatch.setattr(runtime_sync, "RMP_DATA_DIR", str(tmp_path))

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    code = app_dir / "demo.py"
    code.write_text("x = 1\n")

    runtime_sync.mark_runtime_boot("rmp-api")
    runtime_sync.mark_runtime_boot("rmp-worker")
    status = runtime_sync.runtime_sync_status(root=tmp_path)
    assert status["status"] == "ok"

    time.sleep(0.05)
    code.write_text("x = 2\n")
    # Ensure mtime advances even on coarse filesystems
    os.utime(code, (time.time() + 5, time.time() + 5))
    status = runtime_sync.runtime_sync_status(root=tmp_path)
    assert status["status"] == "stale"
    assert "rmp-api" in status["stale_services"]


def test_attempt_remediation_restarts_on_stale_health(monkeypatch):
    from app.production import canary_sentinel
    from app.production.canary_sentinel import CanaryIssue

    calls = []

    monkeypatch.setattr(canary_sentinel, "reap_stale_llm_slots_sync", lambda: [])
    monkeypatch.setattr(canary_sentinel, "_systemctl_is_active", lambda unit: True)
    monkeypatch.setattr(
        canary_sentinel,
        "_restart_unit",
        lambda unit: calls.append(unit) or True,
    )
    monkeypatch.setattr(canary_sentinel, "_remediation_cooldown_active", lambda key="restart_runtime": False)
    monkeypatch.setattr(canary_sentinel, "_record_remediation", lambda key="restart_runtime": None)
    monkeypatch.setattr(canary_sentinel, "count_active_user_tasks_sync", lambda: 0)

    actions = canary_sentinel.attempt_remediation(
        [CanaryIssue("health_canary", "stale", "old")]
    )
    assert "rmp-api" in calls
    assert "rmp-worker" in calls
    assert any("stale-runtime/canary" in a for a in actions)


def test_attempt_remediation_defers_soft_health_when_users_active(monkeypatch):
    from app.production import canary_sentinel
    from app.production.canary_sentinel import CanaryIssue

    calls = []
    monkeypatch.setattr(canary_sentinel, "reap_stale_llm_slots_sync", lambda: [])
    monkeypatch.setattr(canary_sentinel, "_systemctl_is_active", lambda unit: True)
    monkeypatch.setattr(
        canary_sentinel,
        "_restart_unit",
        lambda unit: calls.append(unit) or True,
    )
    monkeypatch.setattr(canary_sentinel, "_remediation_cooldown_active", lambda key="restart_runtime": False)
    monkeypatch.setattr(canary_sentinel, "count_active_user_tasks_sync", lambda: 2)
    monkeypatch.setattr(canary_sentinel, "cancel_task_sync", lambda tid, reason="x": True)

    actions = canary_sentinel.attempt_remediation(
        [CanaryIssue("health_canary", "timeout", "poll timeout", {"task_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"})]
    )
    assert calls == []
    assert any("deferred runtime restart" in a for a in actions)
    assert any("cancelled canary task" in a for a in actions)
