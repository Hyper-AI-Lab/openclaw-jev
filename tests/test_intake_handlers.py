"""Intake handler outcome tests."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.task_registry.intake_handlers import handle_intake_outcome


@pytest.mark.asyncio
async def test_handle_wait_active():
    db = AsyncMock()
    db.add = AsyncMock()
    decision = {
        "effective_decision": "wait_active",
        "decision": "wait_active",
        "target_task_id": "task-1",
        "intake_mode": "enforce",
        "request_hash": "abc",
        "confidence": 90,
        "rationale": "dup",
    }
    with patch(
        "app.task_registry.intake_handlers.record_intake_decision",
        new=AsyncMock(return_value="dec-1"),
    ):
        out = await handle_intake_outcome(
            decision,
            request=None,
            db=db,
            intent="hello",
            session_key="agent:main:main",
            tags=["user-request"],
        )
    assert out["intake_action"] == "wait_active"
    assert out["task_id"] == "task-1"


@pytest.mark.asyncio
async def test_handle_skip():
    db = AsyncMock()
    db.add = AsyncMock()
    decision = {
        "effective_decision": "skip_noop",
        "decision": "skip_noop",
        "intake_mode": "enforce",
        "request_hash": "abc",
        "confidence": 90,
        "rationale": "noop",
    }
    with patch(
        "app.task_registry.intake_handlers.record_intake_decision",
        new=AsyncMock(return_value="dec-1"),
    ):
        out = await handle_intake_outcome(
            decision,
            request=None,
            db=db,
            intent="hello",
            session_key="agent:main:main",
            tags=[],
        )
    assert out["skipped"] is True


@pytest.mark.asyncio
async def test_handle_records_execution_mode_in_event():
    db = AsyncMock()
    db.add = AsyncMock()
    decision = {
        "effective_decision": "create_fresh",
        "decision": "create_fresh",
        "intake_mode": "enforce",
        "request_hash": "abc",
        "confidence": 90,
        "rationale": "chat",
        "execution_mode": "conversational",
        "llm_raw": {"execution_mode": "conversational"},
    }
    recorded = {}

    async def _record(**kwargs):
        recorded.update(kwargs)
        return "dec-1"

    with patch(
        "app.task_registry.intake_handlers.record_intake_decision",
        new=AsyncMock(side_effect=_record),
    ):
        out = await handle_intake_outcome(
            decision,
            request=None,
            db=db,
            intent="hello",
            session_key="agent:main:main",
            tags=["user-request"],
        )
    assert out["execution_mode"] == "conversational"
    assert recorded["llm_raw"]["execution_mode"] == "conversational"
    event = db.add.call_args[0][0]
    assert event.event_payload["execution_mode"] == "conversational"


@pytest.mark.asyncio
async def test_handle_supersede_terminates_and_falls_through():
    db = AsyncMock()
    db.add = AsyncMock()
    old_task = MagicMock()
    old_task.status = "running"
    old_task.next_check_at = None
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = old_task
    db.execute = AsyncMock(return_value=task_result)
    decision = {
        "effective_decision": "supersede",
        "decision": "supersede",
        "target_task_id": "old-task-1",
        "intake_mode": "enforce",
        "request_hash": "abc",
        "confidence": 95,
        "rationale": "stale failed",
    }
    with patch(
        "app.task_registry.intake_handlers.record_intake_decision",
        new=AsyncMock(return_value="dec-1"),
    ), patch(
        "app.temporal_control.terminate_task_workflow",
        new=AsyncMock(return_value=True),
    ) as mock_term:
        out = await handle_intake_outcome(
            decision,
            request=None,
            db=db,
            intent="[cron:test] retry job",
            session_key="agent:main:cron",
            tags=["cron"],
        )
    assert out is None
    mock_term.assert_awaited_once_with("old-task-1", "superseded by intake")
    assert old_task.status == "failed"
