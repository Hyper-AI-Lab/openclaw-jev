"""Tests for runtime code sync detection, and the canary sentinel's remediation (kb/test-gaps.md, "canary-sentinel
remediation"; specs/WP-04.md WP04-23)."""
import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.production import runtime_sync

FAKES = Path(__file__).resolve().parent / "fakes" / "coding"


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


def test_a_plugin_edit_leaves_the_api_and_worker_in_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_sync, "BOOT_DIR", tmp_path)
    monkeypatch.setattr(runtime_sync, "RMP_DATA_DIR", str(tmp_path))
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "demo.py").write_text("x = 1\n")
    plugin = tmp_path / "plugins" / "rmp_adapter" / "claude_tools.js"
    plugin.parent.mkdir(parents=True)
    runtime_sync.mark_runtime_boot("rmp-api")
    runtime_sync.mark_runtime_boot("rmp-worker")
    plugin.write_text("'use strict';\n")
    os.utime(plugin, (time.time() + 5, time.time() + 5))
    assert runtime_sync.runtime_sync_status(root=tmp_path)["status"] == "ok"


@pytest.mark.asyncio
async def test_attempt_remediation_restarts_on_stale_health(monkeypatch, tmp_path):
    from app.production import canary_sentinel
    from app.production.canary_sentinel import CanaryIssue

    calls = []
    state = tmp_path / "last_canary_remediation.json"
    monkeypatch.setattr(canary_sentinel, "REMEDIATION_STATE_PATH", state)

    monkeypatch.setattr(canary_sentinel, "reap_stale_llm_slots_sync", lambda: [])
    monkeypatch.setattr(canary_sentinel, "_systemctl_is_active", lambda unit: True)
    monkeypatch.setattr(
        canary_sentinel,
        "_restart_unit",
        lambda unit: calls.append(unit) or True,
    )
    monkeypatch.setattr(canary_sentinel, "_remediation_cooldown_active", lambda key="restart_runtime": False)
    monkeypatch.setattr(canary_sentinel, "count_active_user_tasks_sync", lambda: 0)
    monkeypatch.setattr(canary_sentinel, "cancel_stuck_canary_tasks", AsyncMock(return_value=[]))
    monkeypatch.setattr(canary_sentinel, "cancel_task", AsyncMock(return_value=True))

    actions = await canary_sentinel.attempt_remediation(
        [CanaryIssue("health_canary", "stale", "old")]
    )
    assert "rmp-api" in calls
    assert "rmp-worker" in calls
    assert any("stale-runtime/canary" in a for a in actions)
    # The restart is recorded, which starts the cooldown.
    assert list(json.loads(state.read_text())) == ["restart_runtime"]


@pytest.mark.asyncio
async def test_attempt_remediation_defers_soft_health_when_users_active(monkeypatch):
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
    cancel = AsyncMock(return_value=True)
    monkeypatch.setattr(canary_sentinel, "cancel_task", cancel)
    monkeypatch.setattr(canary_sentinel, "cancel_stuck_canary_tasks", AsyncMock(return_value=[]))

    actions = await canary_sentinel.attempt_remediation(
        [CanaryIssue("health_canary", "timeout", "poll timeout", {"task_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"})]
    )
    assert calls == []
    assert any("cancelled canary task" in a for a in actions)
    # The sentinel is a short-lived process: its cancel waits for Aura's runs to abort.
    cancel.assert_awaited_once_with("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", reason="health_canary_timeout",
                                    actor="canary_sentinel", wait_abort=True)


@pytest.mark.asyncio
async def test_attempt_remediation_soft_timeout_idle_does_not_restart(monkeypatch):
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
    monkeypatch.setattr(canary_sentinel, "count_active_user_tasks_sync", lambda: 0)
    monkeypatch.setattr(canary_sentinel, "cancel_task", AsyncMock(return_value=True))
    monkeypatch.setattr(canary_sentinel, "cancel_stuck_canary_tasks", AsyncMock(return_value=[]))

    await canary_sentinel.attempt_remediation(
        [CanaryIssue("health_canary", "timeout", "poll timeout", {"task_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"})]
    )
    assert calls == []


@pytest.fixture
def fake_systemctl(tmp_path, monkeypatch):
    """tests/fakes/coding/systemctl first on PATH, with every unit the sentinel watches running. Returns the units'
    pid directory (remove a unit's pid file to take it down) and a reader of the restarts the fake was asked for."""
    from app.production import canary_sentinel

    units, log = tmp_path / "units", tmp_path / "systemctl.log"
    units.mkdir()
    for unit in canary_sentinel.SERVICE_UNITS:
        (units / f"{unit}.pid").write_text(str(os.getpid()))
    monkeypatch.setenv("PATH", f"{FAKES}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_UNITS_DIR", str(units))
    monkeypatch.setenv("FAKE_SYSTEMCTL_LOG", str(log))
    monkeypatch.setattr(canary_sentinel, "reap_stale_llm_slots_sync", lambda: [])
    monkeypatch.setattr(canary_sentinel, "count_active_user_tasks_sync", lambda: 0)
    monkeypatch.setattr(canary_sentinel, "cancel_task", AsyncMock(return_value=True))
    monkeypatch.setattr(canary_sentinel, "cancel_stuck_canary_tasks", AsyncMock(return_value=[]))

    def restarts():
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if line.startswith("restart ")]

    return units, restarts


@pytest.mark.asyncio
async def test_a_down_unit_is_restarted_through_systemctl(fake_systemctl):
    from app.production import canary_sentinel
    from app.production.canary_sentinel import CanaryIssue

    units, restarts = fake_systemctl
    (units / "rmp-api.pid").unlink()
    # §0: the sentinel's systemctl is the fake, never this host's.
    assert shutil.which("systemctl") == str(FAKES / "systemctl")

    actions = await canary_sentinel.attempt_remediation([CanaryIssue("health_canary", "timeout", "poll timeout")])

    assert restarts() == ["restart rmp-api.service"]
    assert "restarted rmp-api" in actions


@pytest.mark.asyncio
async def test_the_cooldown_blocks_a_second_restart(fake_systemctl, tmp_path, monkeypatch):
    from app.production import canary_sentinel
    from app.production.canary_sentinel import CanaryIssue

    _units, restarts = fake_systemctl
    state = tmp_path / "last_canary_remediation.json"
    monkeypatch.setattr(canary_sentinel, "REMEDIATION_STATE_PATH", state)
    assert shutil.which("systemctl") == str(FAKES / "systemctl")
    stale = [CanaryIssue("runtime_code_sync", "stale", "On-disk code newer than running process")]
    once = ["restart rmp-api.service", "restart rmp-worker.service"]

    await canary_sentinel.attempt_remediation(stale)
    assert restarts() == once

    await canary_sentinel.attempt_remediation(stale)
    assert restarts() == once, "a second stale issue within the cooldown restarts nothing"

    # The last restart was 21 minutes ago: the 20-minute cooldown is over.
    earlier = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=21)
    state.write_text(json.dumps({"restart_runtime": earlier.isoformat() + "Z"}))
    await canary_sentinel.attempt_remediation(stale)
    assert restarts() == once + once


@pytest.mark.asyncio
async def test_stuck_canaries_are_cancelled_through_cancel_task(tmp_path, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app import temporal_control
    from app.db.models import Base, Event, ProcessRun, Task
    from app.production import canary_sentinel

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rmp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Task.__table__, ProcessRun.__table__, Event.__table__])
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add_all([
            Task(id="t-health", goal="RMP CANARY: Reply with exactly CANARY_OK", task_type="canary",
                 status="running"),
            Task(id="t-memory", goal="MEMORY CANARY: which fact was seeded?", task_type="user", status="created"),
            Task(id="t-done", goal="RMP CANARY: Reply with exactly CANARY_OK", task_type="canary",
                 status="completed"),
            Task(id="t-user", goal="Plan the trip to Kyoto", task_type="user", status="running"),
        ])
        await db.commit()
    client = MagicMock()
    client.get_workflow_handle.return_value.signal = AsyncMock()
    client.get_workflow_handle.return_value.terminate = AsyncMock()
    cancel = AsyncMock(wraps=temporal_control.cancel_task)
    for module in (temporal_control, canary_sentinel):
        monkeypatch.setattr(module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(temporal_control, "connect_temporal", AsyncMock(return_value=client))
    monkeypatch.setattr("app.openclaw_control.abort_task_runs", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.task_registry.hooks.index_terminal_task_async", AsyncMock())
    monkeypatch.setattr(canary_sentinel, "cancel_task", cancel)
    try:
        cancelled = await canary_sentinel.cancel_stuck_canary_tasks("canary_sentinel_recovery")
        async with factory() as db:
            statuses = {task.id: task.status for task in (await db.execute(select(Task))).scalars()}
    finally:
        await engine.dispose()

    assert sorted(cancelled) == ["t-health", "t-memory"]
    assert sorted(call.args[0] for call in cancel.await_args_list) == ["t-health", "t-memory"]
    assert all(call.kwargs == {"reason": "canary_sentinel_recovery", "actor": "canary_sentinel", "wait_abort": True}
               for call in cancel.await_args_list)
    assert statuses == {"t-health": "cancelled", "t-memory": "cancelled", "t-done": "completed", "t-user": "running"}
    assert sorted(call.args[0] for call in client.get_workflow_handle.call_args_list) == [
        "workflow-t-health", "workflow-t-memory"]
