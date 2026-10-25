"""Control-plane tests: status and compensation on a real database, reconciler filter, fail-soft memory.

The status activities run on a SQLite session, as a workflow schedules them; a /cancel of a finished task is refused
through the same writer (tests/test_task_cancel_api.py).
"""
import dataclasses
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment

from app.activities import db_activities
from app.db.models import Base, Event, MemoryItem, ProcessRun, Step, Task
from app.notification_policy import is_internal_task


@pytest.fixture
async def control_plane(tmp_path, monkeypatch):
    monkeypatch.setenv("RMP_METRICS_PATH", str(tmp_path / "metrics.json"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'control.db'}")
    tables = [model.__table__ for model in (Task, ProcessRun, Step, Event, MemoryItem)]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(db_activities, "AsyncSessionLocal", factory):
        yield factory
    await engine.dispose()


async def seed(factory, *rows):
    async with factory() as db:
        db.add_all(rows)
        await db.commit()


async def rows(factory, model, *where):
    async with factory() as db:
        return (await db.execute(select(model).where(*where))).scalars().all()


def in_workflow(workflow_type="GenericTaskWorkflow"):
    """An activity context as the worker gives one, scheduled by ``workflow_type``."""
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, workflow_type=workflow_type)
    return env


def later(minutes):
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes)


async def test_compensation_ends_the_run_the_task_and_their_open_steps(control_plane):
    await seed(control_plane,
               Task(id="t1", goal="Compare visa rules", status="running", next_check_at=later(30)),
               ProcessRun(id="r1", task_id="t1", current_state="running", lease_owner="t1", next_check_at=later(5)),
               Step(id="s-running", process_run_id="r1", status="running"),
               Step(id="s-pending", process_run_id="r1", status="pending"),
               Step(id="s-done", process_run_id="r1", status="completed"))
    notes = []

    async def write(**kwargs):
        notes.append(kwargs)
        return "mem-1"

    with patch("app.memory.router.MemoryRouter.write", new=staticmethod(write)):
        result = await in_workflow().run(db_activities.execute_compensation,
                                         {"task_id": "t1", "process_run_id": "r1", "reason": "step failed twice"})

    assert result == {"compensated": True, "steps_marked": 2, "task_id": "t1", "process_run_id": "r1"}
    (task,) = await rows(control_plane, Task)
    (run,) = await rows(control_plane, ProcessRun)
    assert task.status == "compensated" and task.next_check_at is None
    assert run.current_state == "compensated" and run.ended_at is not None and run.lease_owner is None
    assert run.next_check_at is None
    steps = {step.id: step for step in await rows(control_plane, Step)}
    assert {sid: step.status for sid, step in steps.items()} == {
        "s-running": "compensated", "s-pending": "compensated", "s-done": "completed"}
    assert steps["s-running"].ended_at is not None and steps["s-done"].ended_at is None
    (event,) = await rows(control_plane, Event)
    assert event.event_type == "task.compensated" and event.entity_id == "t1"
    assert event.event_payload == {"reason": "step failed twice", "process_run_id": "r1", "steps_marked": 2}
    assert notes == [{"scope_type": "process", "scope_id": "r1", "memory_type": "working",
                      "content": "Compensated: step failed twice",
                      "provenance": {"task_id": "t1", "compensation": True}, "confidence": 100}]


async def test_compensating_a_stopped_task_changes_nothing(control_plane, caplog):
    """Kirill's stop came first: the late compensation leaves the task, its run and its steps as they are, writes no
    event and no memory note, and logs both refusals."""
    caplog.set_level(logging.WARNING, logger="rmp.transitions")
    await seed(control_plane,
               Task(id="t1", goal="Compare visa rules", status="stopped_by_user"),
               ProcessRun(id="r1", task_id="t1", current_state="stopped_by_user"),
               Step(id="s-running", process_run_id="r1", status="running"))
    write = AsyncMock(return_value="mem-1")

    with patch("app.memory.router.MemoryRouter.write", new=staticmethod(write)):
        result = await in_workflow().run(db_activities.execute_compensation,
                                         {"task_id": "t1", "process_run_id": "r1", "reason": "late error"})

    assert result["compensated"] is False and result["steps_marked"] == 0
    assert [t.status for t in await rows(control_plane, Task)] == ["stopped_by_user"]
    assert [r.current_state for r in await rows(control_plane, ProcessRun)] == ["stopped_by_user"]
    assert [s.status for s in await rows(control_plane, Step)] == ["running"]
    assert await rows(control_plane, Event) == []
    write.assert_not_awaited()
    assert [r.getMessage() for r in caplog.records if r.name == "rmp.transitions"] == [
        "run transition refused: r1 stopped_by_user -> compensated actor=workflow:GenericTaskWorkflow "
        "cause=execute_compensation",
        "task transition refused: t1 stopped_by_user -> compensated actor=workflow:GenericTaskWorkflow "
        "cause=execute_compensation",
    ]


@pytest.mark.parametrize("finished", ["failed", "cancelled", "completed"])
async def test_a_late_running_does_not_resurrect_a_finished_task(control_plane, caplog, finished):
    """test-gaps #7: a workflow's late "running" after /cancel, a repair or its own close is refused and logged."""
    caplog.set_level(logging.WARNING, logger="rmp.transitions")
    await seed(control_plane, Task(id="t1", goal="Compare visa rules", status=finished))

    with patch("app.task_registry.hooks.index_terminal_task_async", new_callable=AsyncMock) as index:
        changed = await in_workflow().run(db_activities.update_task_status,
                                          {"task_id": "t1", "status": "running", "next_check_minutes": 60})

    assert changed is False
    (task,) = await rows(control_plane, Task)
    assert task.status == finished and task.next_check_at is None
    index.assert_not_awaited()
    assert [r.getMessage() for r in caplog.records if r.name == "rmp.transitions"] == [
        f"task transition refused: t1 {finished} -> running actor=workflow:GenericTaskWorkflow "
        "cause=update_task_status"]


@pytest.mark.parametrize("status, state", [("completed", "completed"), ("failed", "failed_terminal")])
async def test_the_deploy_reply_run_closes_its_deploying_task(control_plane, status, state):
    """The deploy's reply path: after a self-deploy, ops/coding_deploy.py starts a fresh CodingTaskWorkflow run on
    the task the coding workflow handed off as deploying; that run's close goes through the guarded writer."""
    await seed(control_plane, Task(id="t1", goal="Fix the greeting", task_type="coding_task", status="deploying"),
               ProcessRun(id="r1", task_id="t1", process_type="coding_task", current_state="deploying",
                          lease_owner="t1"))
    reply_run = in_workflow("CodingTaskWorkflow")

    with patch("app.task_registry.hooks.index_terminal_task_async", new_callable=AsyncMock) as index, \
         patch.object(db_activities, "send_alert", new_callable=AsyncMock):
        assert await reply_run.run(db_activities.ensure_process_run,
                                   {"task_id": "t1", "process_type": "coding_task"}) == "r1"
        assert await reply_run.run(db_activities.update_task_status, {"task_id": "t1", "status": status}) is True
        assert await reply_run.run(db_activities.update_process_state,
                                   {"process_run_id": "r1", "state": state, "ended": True}) is True

    (task,) = await rows(control_plane, Task)
    (run,) = await rows(control_plane, ProcessRun)
    assert task.status == status and run.current_state == state and run.lease_owner is None
    index.assert_awaited_once_with("t1")


async def test_side_effects_run_once(control_plane):
    """A second "completed" is no transition: the registry index, the metric and the alert run for the first only."""
    from app import metrics

    await seed(control_plane, Task(id="t1", goal="Compare visa rules", status="running"),
               Task(id="t2", goal="Compare visa rules", status="running"))
    with patch("app.task_registry.hooks.index_terminal_task_async", new_callable=AsyncMock) as index, \
         patch.object(db_activities, "send_alert", new_callable=AsyncMock) as alert:
        first = await in_workflow().run(db_activities.update_task_status, {"task_id": "t1", "status": "completed"})
        second = await in_workflow().run(db_activities.update_task_status, {"task_id": "t1", "status": "completed"})
        failed = await in_workflow().run(db_activities.update_task_status, {"task_id": "t2", "status": "failed"})
        again = await in_workflow().run(db_activities.finalize_task_failure, {"task_id": "t2"})

    assert (first, second, failed, again) == (True, False, True, False)
    assert [c.args[0] for c in index.await_args_list] == ["t1", "t2"]
    assert alert.await_count == 1
    counters = metrics.get_counters()
    assert counters["task_completed"] == 1 and counters["task_failed"] == 1


async def test_a_direct_call_names_its_actor(control_plane, caplog):
    """Outside an activity, the caller names itself: the reconciler, spawn, a script."""
    caplog.set_level(logging.WARNING, logger="rmp.transitions")
    await seed(control_plane, Task(id="t1", goal="Compare visa rules", status="completed"))

    assert await db_activities.finalize_task_failure(
        {"task_id": "t1", "actor": "reconciler", "cause": "orphaned catalog run"}) is False
    with pytest.raises(KeyError):
        await db_activities.update_task_status({"task_id": "t1", "status": "running"})
    assert [r.getMessage() for r in caplog.records if r.name == "rmp.transitions"] == [
        "task transition refused: t1 completed -> failed actor=reconciler cause=orphaned catalog run"]


def test_reconciler_skips_internal_tasks():
    assert is_internal_task("[INTERNAL_RMP] heartbeat", "heartbeat", ["cron"]) is True
    assert is_internal_task("Summarize the weekly report", "user", []) is False


@pytest.mark.asyncio
async def test_read_process_memory_fail_soft(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "app.memory.router.MemoryRouter.read_ordered",
        staticmethod(boom),
    )
    from app.activities.db_activities import read_process_memory

    assert await read_process_memory({"ordered": True, "scope_id": "pr-1"}) == []


@pytest.mark.asyncio
async def test_quota_broker_mutate_state(monkeypatch, tmp_path):
    from pathlib import Path

    from app.llm import quota_broker

    state_file = Path(tmp_path / "quota_state.json")
    monkeypatch.setattr(quota_broker, "STATE_PATH", state_file)

    def mutator(data):
        data["calls"] = data.get("calls", 0) + 1
        return data

    quota_broker._mutate_state(mutator)
    quota_broker._mutate_state(mutator)
    import json

    data = json.loads(state_file.read_text())
    assert data["calls"] == 2
