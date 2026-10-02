"""Coding tasks in the reconciler: an orphan gets its units stopped and a notice, and is never re-judged."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app import reconciler


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


async def _close(client, db):
    stats = {"events": 0}
    with patch("app.activities.coding_activities.stop_task_units", return_value=["aura-claude-t1-2"]) as stop, \
         patch("app.activities.db_activities.finalize_task_failure", new_callable=AsyncMock) as fail, \
         patch.object(reconciler, "_notify_repair", new_callable=AsyncMock) as notify:
        closed = await reconciler._close_orphaned_coding_tasks(client, db, datetime.utcnow(), stats)
    return closed, stop, fail, notify, db.add.call_args_list


@pytest.mark.parametrize("client", [_client(missing=True), _client(WorkflowExecutionStatus.TERMINATED),
                                    _client(WorkflowExecutionStatus.FAILED)])
async def test_a_coding_task_without_its_workflow_is_stopped_and_closed_with_a_notice(client):
    task = _task(status="pending_user_input")
    closed, stop, fail, notify, added = await _close(client, _db([task]))

    assert closed == ["t1"] and task.status == "failed"
    stop.assert_called_once_with("t1")
    assert fail.await_args.args[0] == {"task_id": "t1", "task_status": "failed", "process_state": "failed_terminal"}
    message = notify.await_args.args[1]
    assert "lost its workflow, so I closed it and stopped its Claude Code run" in message and "Nothing shipped" in message
    assert added[0].args[0].event_type == "reconciler.coding_orphan_closed"
    client.start_workflow.assert_not_awaited()


async def test_a_running_or_just_touched_coding_task_is_left_alone():
    for client, task in ((_client(WorkflowExecutionStatus.RUNNING), _task()), (_client(missing=True), _task(minutes=0))):
        closed, stop, fail, notify, added = await _close(client, _db([task]))
        assert closed == [] and task.status != "failed" and not stop.called and notify.await_count == 0 and not added


async def test_a_deploying_task_belongs_to_its_deploy_unit_until_it_reports_back():
    client = _client(WorkflowExecutionStatus.COMPLETED)
    for minutes, live in ((30, False), (300, True)):
        task = _task(status="deploying", minutes=minutes)
        with patch.object(reconciler, "runner_unit_active", return_value=live):
            closed, stop, fail, notify, added = await _close(client, _db([task]))
        assert closed == [] and task.status == "deploying" and notify.await_count == 0

    task = _task(status="deploying", minutes=300)
    with patch.object(reconciler, "runner_unit_active", return_value=False):
        closed, stop, fail, notify, added = await _close(client, _db([task]))
    assert closed == ["t1"] and task.status == "failed"
    assert "deploy never reported back" in notify.await_args.args[1]


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
