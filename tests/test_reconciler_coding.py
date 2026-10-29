"""Coding tasks in the reconciler: an orphan gets its units stopped and a notice, and is never re-judged."""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app import reconciler
from app.activities import db_activities
from app.db.models import Base, Event, ProcessRun, Task


def _task(status="running", minutes=5, task_type="user"):
    return SimpleNamespace(id="t1", status=status, updated_at=datetime.utcnow() - timedelta(minutes=minutes),
                           correlation_id="t1", goal="Fix the greeting", task_type=task_type, task_kind="one_shot",
                           next_check_at=None, openclaw_session_key="agent:main:slack:channel:d0test")


def _db(tasks=(), coding=True):
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = list(tasks)
    rows.scalars.return_value.first.return_value = "run-1" if coding else None
    db = MagicMock()
    db.execute = AsyncMock(return_value=rows)
    return db


def _client(status=None, missing=False):
    handle = MagicMock()
    error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"") if missing else None
    handle.describe = AsyncMock(return_value=SimpleNamespace(status=status, workflow_type="CodingTaskWorkflow"),
                                side_effect=error)
    handle.terminate = AsyncMock()
    client = MagicMock()
    client.get_workflow_handle.return_value = handle
    client.start_workflow = AsyncMock()
    return client


@pytest.fixture
async def factory(tmp_path):
    """A SQLite file the reconciler's pass and finalize_task_failure's own session share."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'coding.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Task.__table__, ProcessRun.__table__, Event.__table__])
    sessions = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(db_activities, "AsyncSessionLocal", sessions), \
         patch("app.task_registry.hooks.index_terminal_task_async", new_callable=AsyncMock):
        yield sessions
    await engine.dispose()


async def _close(client, factory, status="running", minutes=5):
    """One coding task t1 with its run, then the reconciler's orphan pass. Returns what the pass closed, its fakes,
    and the task's status, its run's state and the events after the pass's commit."""
    async with factory() as db:
        db.add_all([Task(id="t1", goal="Fix the greeting", task_type="user", status=status,
                         updated_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes)),
                    ProcessRun(id="r1", task_id="t1", process_type="coding_task", current_state="running")])
        await db.commit()
    stats = {"events": 0}
    with patch("app.activities.coding_activities.stop_task_units", return_value=["aura-claude-t1-2"]) as stop, \
         patch.object(reconciler, "_notify_repair", new_callable=AsyncMock) as notify:
        async with factory() as db:
            closed = await reconciler._close_orphaned_coding_tasks(client, db, datetime.now(timezone.utc).replace(tzinfo=None), stats)
            await db.commit()
    async with factory() as db:
        task = await db.get(Task, "t1")
        run = await db.get(ProcessRun, "r1")
        events = (await db.execute(select(Event))).scalars().all()
        await db.execute(Task.__table__.delete())
        await db.execute(ProcessRun.__table__.delete())
        await db.execute(Event.__table__.delete())
        await db.commit()
    return closed, stop, notify, (task.status, run.current_state), events


@pytest.mark.parametrize("client", [_client(missing=True), _client(WorkflowExecutionStatus.TERMINATED),
                                    _client(WorkflowExecutionStatus.FAILED)])
async def test_a_coding_task_without_its_workflow_is_stopped_and_closed_with_a_notice(client, factory):
    closed, stop, notify, states, events = await _close(client, factory, status="pending_user_input")

    assert closed == ["t1"] and states == ("failed", "running")
    stop.assert_called_once_with("t1")
    message = notify.await_args.args[1]
    assert "lost its workflow, so I closed it and stopped its Claude Code run" in message and "Nothing shipped" in message
    describe = client.get_workflow_handle.return_value.describe
    workflow_status = "NOT_FOUND" if describe.side_effect else describe.return_value.status.name
    assert [(e.event_type, e.event_payload) for e in events] == [(
        "reconciler.coding_orphan_closed", {"workflow_status": workflow_status, "stopped_units": ["aura-claude-t1-2"]})]
    client.start_workflow.assert_not_awaited()


async def test_a_running_or_just_touched_coding_task_is_left_alone(factory):
    for client, minutes in ((_client(WorkflowExecutionStatus.RUNNING), 5), (_client(missing=True), 0)):
        closed, stop, notify, states, events = await _close(client, factory, minutes=minutes)
        assert closed == [] and states == ("running", "running") and not stop.called and notify.await_count == 0
        assert events == []


async def test_a_deploying_task_belongs_to_its_deploy_unit_until_it_reports_back(factory):
    client = _client(WorkflowExecutionStatus.COMPLETED)
    for minutes, live in ((30, False), (300, True)):
        with patch.object(reconciler, "runner_unit_active", return_value=live):
            closed, stop, notify, states, events = await _close(client, factory, status="deploying", minutes=minutes)
        assert closed == [] and states == ("deploying", "running") and notify.await_count == 0

    with patch.object(reconciler, "runner_unit_active", return_value=False):
        closed, stop, notify, states, events = await _close(client, factory, status="deploying", minutes=300)
    assert closed == ["t1"] and states[0] == "failed"
    assert "deploy never reported back" in notify.await_args.args[1]


async def test_a_deploying_task_whose_run_still_waits_is_not_closed(factory, tmp_path, monkeypatch):
    from app.coding import deploy

    monkeypatch.setattr(deploy, "RUNS_DIR", tmp_path / "runs")
    deploy.queue({"task_id": "t1", "old": "b" * 40, "head": "c" * 40})
    client = _client(WorkflowExecutionStatus.COMPLETED)
    with patch.object(reconciler, "runner_unit_active", return_value=False):
        closed, stop, notify, states, events = await _close(client, factory, status="deploying", minutes=300)
        assert closed == [] and states == ("deploying", "running") and notify.await_count == 0 and events == []

        deploy.state_file("t1").unlink()
        closed, stop, notify, states, events = await _close(client, factory, status="deploying", minutes=300)
    assert closed == ["t1"] and states[0] == "failed"
    assert "deploy never reported back" in notify.await_args.args[1]


async def test_a_coding_task_that_finished_meanwhile_is_left_as_it_ended(factory, caplog):
    """The workflow's own close lands while the reconciler looks for it: the reconciler's failure is refused, and it
    sends no notice and records no closing."""
    caplog.set_level(logging.WARNING, logger="rmp.transitions")
    client = _client()

    async def gone_after_its_close(*args):
        async with factory() as db:
            (await db.get(Task, "t1")).status = "completed"
            await db.commit()
        raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    client.get_workflow_handle.return_value.describe = AsyncMock(side_effect=gone_after_its_close)

    closed, stop, notify, states, events = await _close(client, factory, status="running")

    assert closed == [] and states[0] == "completed" and events == [] and notify.await_count == 0
    assert [r.getMessage() for r in caplog.records if r.name == "rmp.transitions"] == [
        "task transition refused: t1 completed -> failed actor=reconciler cause=coding workflow NOT_FOUND"]


async def test_an_orphaned_coding_reply_is_never_re_judged():
    client = _client(WorkflowExecutionStatus.FAILED)
    db = _db()
    db.execute.return_value.scalars.return_value.first.side_effect = [None, "run-1"]
    with patch("app.orchestrator.session_recovery.read_completed_rmp_session_reply", return_value="A long finished reply " * 5):
        outcome = await reconciler._recover_orphaned_session_reply(client, db, _task(minutes=3), datetime.utcnow(), {"events": 0})
    assert outcome is None
    client.start_workflow.assert_not_awaited()


async def test_the_stuck_repair_waits_while_a_coding_unit_is_alive():
    client = _client(WorkflowExecutionStatus.RUNNING)
    with patch("app.activities.coding_activities.live_task_units", return_value=["aura-claude-t1-1.service"]):
        repaired = await reconciler._repair_stuck_running_task(client, _db(), _task(minutes=60), datetime.utcnow(), {"repaired": 0})
    assert repaired is False
    client.get_workflow_handle.return_value.terminate.assert_not_awaited()


async def test_the_stuck_repair_of_a_coding_task_stops_its_units():
    client = _client(WorkflowExecutionStatus.RUNNING)
    task = _task(minutes=60)
    with patch("app.activities.coding_activities.live_task_units", return_value=[]), \
         patch("app.activities.coding_activities.stop_task_units", return_value=[]) as stop, \
         patch("app.activities.db_activities.finalize_task_failure", new_callable=AsyncMock), \
         patch("app.activities.db_activities.execute_compensation", new_callable=AsyncMock), \
         patch.object(reconciler, "_cleanup_orphan_plan_children", new_callable=AsyncMock, return_value=0), \
         patch.object(reconciler, "_notify_repair", new_callable=AsyncMock):
        repaired = await reconciler._repair_stuck_running_task(client, _db(), task, datetime.utcnow(), {"repaired": 0, "events": 0})
    assert repaired is True
    client.get_workflow_handle.return_value.terminate.assert_awaited_once()
    stop.assert_called_once_with("t1")
