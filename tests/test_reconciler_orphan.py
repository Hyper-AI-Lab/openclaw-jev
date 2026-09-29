"""Orphaned replies are re-judged by a restarted run, never posted by the reconciler."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app import reconciler

REPLY = "Here is the summary you asked for: three items need your attention today."
ORIGINAL = {"task_id": "t1", "intent": "summarize", "session_key": "s", "task_type": "user",
            "tags": ["user-request"], "initial_memory_block": "RECENT DIALOGUE ..."}


def _task(task_type="user"):
    return SimpleNamespace(
        id="t1", status="running", updated_at=datetime.utcnow() - timedelta(minutes=3),
        correlation_id="t1", goal="summarize", task_type=task_type, task_kind="one_shot",
        next_check_at=None, openclaw_session_key="agent:main:slack:channel:d0test",
    )


def _db(prior_events=()):
    rows = MagicMock()
    rows.scalars.return_value.first.return_value = prior_events[0] if prior_events else None
    db = MagicMock()
    db.execute = AsyncMock(return_value=rows)
    return db


def _client(desc=None, describe_error=None):
    handle = MagicMock()
    handle.describe = AsyncMock(return_value=desc, side_effect=describe_error)
    started = SimpleNamespace(input=SimpleNamespace(payloads=["p"]))
    handle.fetch_history = AsyncMock(return_value=SimpleNamespace(
        events=[SimpleNamespace(workflow_execution_started_event_attributes=started)]))
    client = MagicMock()
    client.get_workflow_handle.return_value = handle
    client.data_converter.decode = AsyncMock(return_value=[dict(ORIGINAL)])
    client.start_workflow = AsyncMock()
    return client


async def _recover(client, db, task):
    stats = {"events": 0}
    with patch("app.orchestrator.session_recovery.read_completed_rmp_session_reply", return_value=REPLY), \
         patch("app.orchestrator.session_recovery.extract_user_facing_reply", side_effect=lambda r: r), \
         patch.object(reconciler, "_notify_repair", new_callable=AsyncMock) as notify, \
         patch("app.activities.db_activities.finalize_task_failure", new_callable=AsyncMock) as fail:
        outcome = await reconciler._recover_orphaned_session_reply(client, db, task, datetime.utcnow(), stats)
    return outcome, notify, fail, db.add.call_args_list


@pytest.mark.asyncio
async def test_a_running_workflow_is_left_alone():
    client = _client(desc=SimpleNamespace(status=WorkflowExecutionStatus.RUNNING, workflow_type="GenericTaskWorkflow"))
    outcome, notify, _, added = await _recover(client, _db(), _task())
    assert outcome is None and not added
    client.start_workflow.assert_not_awaited()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dead_generic_run_restarts_from_its_input_with_the_draft_to_judge():
    client = _client(desc=SimpleNamespace(status=WorkflowExecutionStatus.FAILED, workflow_type="GenericTaskWorkflow"))
    outcome, notify, _, added = await _recover(client, _db(), _task())
    assert outcome == "restarted"
    args, kwargs = client.start_workflow.await_args
    assert args[0] == "GenericTaskWorkflow" and kwargs["id"] == "workflow-t1"
    assert args[1] == {**ORIGINAL, "recovered_draft": REPLY}
    notify.assert_not_awaited()
    assert added[0].args[0].event_type == "reconciler.orphan_reply_rejudged"


@pytest.mark.asyncio
async def test_a_missing_run_restarts_from_the_task_row():
    missing = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    client = _client(describe_error=missing)
    outcome, _, _, _ = await _recover(client, _db(), _task())
    assert outcome == "restarted"
    payload = client.start_workflow.await_args.args[1]
    assert payload["intent"] == "summarize" and payload["recovered_draft"] == REPLY
    assert payload["escalate_user_attempt"] >= payload["strategy_change_attempt"]


@pytest.mark.asyncio
async def test_a_dead_catalog_run_is_closed_with_a_notice_not_replayed():
    client = _client(desc=SimpleNamespace(status=WorkflowExecutionStatus.TERMINATED, workflow_type="CatalogTaskWorkflow"))
    task = _task()
    outcome, notify, fail, added = await _recover(client, _db(), task)
    assert outcome == "failed" and task.status == "failed"
    client.start_workflow.assert_not_awaited()
    fail.assert_awaited_once()
    assert "did not send it" in notify.await_args.args[1]
    assert added[0].args[0].event_type == "reconciler.orphan_run_failed"


@pytest.mark.asyncio
async def test_prior_recovery_or_unreachable_temporal_does_nothing():
    closed = SimpleNamespace(status=WorkflowExecutionStatus.COMPLETED, workflow_type="GenericTaskWorkflow")
    outcome, _, _, _ = await _recover(_client(desc=closed), _db(prior_events=[object()]), _task())
    assert outcome is None
    down = RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
    client = _client(describe_error=down)
    outcome, _, _, _ = await _recover(client, _db(), _task())
    assert outcome is None
    client.start_workflow.assert_not_awaited()
