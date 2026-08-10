"""Spawn process tests."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_session(task, latest_run):
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = task
    pr_result = MagicMock()
    pr_result.scalar_one_or_none.return_value = latest_run
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[task_result, pr_result])
    return session


@pytest.mark.asyncio
async def test_spawn_active_non_durable_returns_exists():
    from app.task_registry.spawn import spawn_process_for_task

    task = MagicMock()
    task.id = "t1"
    task.task_kind = "one_shot"
    task.task_type = "user"
    task.goal = "test"
    task.status = "running"
    task.parent_task_id = None
    task.openclaw_session_key = "agent:main:main"
    task.correlation_id = "t1"

    run = MagicMock()
    run.current_state = "running"

    with patch(
        "app.task_registry.spawn.workflow_is_running",
        new=AsyncMock(return_value=False),
    ):
        out = await spawn_process_for_task("t1", db=_mock_session(task, run))
    assert out.get("reason") == "active_process_exists"
    assert out.get("spawned") is False


@pytest.mark.asyncio
async def test_spawn_durable_running_signals_spawn_leg():
    from app.task_registry.spawn import spawn_process_for_task

    task = MagicMock()
    task.id = "t-durable"
    task.task_kind = "durable"
    task.task_type = "user"
    task.goal = "leg one"
    task.status = "running"
    task.openclaw_session_key = "agent:main:main"
    task.correlation_id = "t-durable"

    run = MagicMock()
    run.current_state = "running"
    run.id = "pr-old"

    with patch(
        "app.task_registry.spawn.workflow_is_running",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.task_registry.spawn.ensure_process_run",
        new=AsyncMock(return_value="pr-new"),
    ) as mock_ensure, patch(
        "app.task_registry.spawn.signal_spawn_leg",
        new=AsyncMock(return_value=True),
    ) as mock_signal:
        out = await spawn_process_for_task(
            "t-durable",
            leg_intent="leg two",
            db=_mock_session(task, run),
        )

    assert out.get("spawned") is True
    assert out.get("spawn_leg_signaled") is True
    assert out.get("workflow_started") is False
    mock_ensure.assert_awaited_once()
    assert mock_ensure.await_args[0][0].get("durable_leg") is True
    mock_signal.assert_awaited_once_with("t-durable", "pr-new", "leg two")


@pytest.mark.asyncio
async def test_spawn_terminal_prior_leg_starts_workflow():
    from app.task_registry.spawn import spawn_process_for_task

    task = MagicMock()
    task.id = "t2"
    task.task_kind = "durable"
    task.task_type = "user"
    task.goal = "resume work"
    task.status = "completed"
    task.openclaw_session_key = "agent:main:main"
    task.correlation_id = "t2"

    run = MagicMock()
    run.current_state = "completed"
    run.id = "pr-done"

    session = _mock_session(task, run)

    with patch(
        "app.task_registry.spawn.workflow_is_running",
        new=AsyncMock(return_value=False),
    ), patch(
        "app.task_registry.spawn.ensure_process_run",
        new=AsyncMock(return_value="pr-next"),
    ), patch(
        "app.task_registry.spawn.start_task_workflow",
        new=AsyncMock(),
    ) as mock_start:
        out = await spawn_process_for_task("t2", db=session)

    assert out.get("spawned") is True
    assert out.get("workflow_started") is True
    mock_start.assert_awaited_once()
