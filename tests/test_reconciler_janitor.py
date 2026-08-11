"""Reconciler and workflow janitor tests (W5)."""
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.reconciler import (
    STALE_TASK_MINUTES,
    STUCK_REPAIR_MINUTES,
    _cleanup_orphan_plan_children,
    _task_is_internal,
    count_stuck_running_workflows,
)


def test_stale_threshold_tuned():
    assert STALE_TASK_MINUTES == 20
    assert STUCK_REPAIR_MINUTES == 45


def test_internal_task_skips_notify_spam():
    class Task:
        goal = "RMP CANARY test"
        task_type = "canary"

    assert _task_is_internal(Task) is True

    class SmokeTask:
        goal = "Intake attach smoke 20260608: research topic"
        task_type = "user"

    assert _task_is_internal(SmokeTask) is True


@pytest.mark.asyncio
async def test_cleanup_orphan_plan_children():
    client = MagicMock()
    running_wf = MagicMock()
    running_wf.id = "task-1-plan-step-1"
    running_wf.status = __import__(
        "temporalio.client", fromlist=["WorkflowExecutionStatus"]
    ).WorkflowExecutionStatus.RUNNING

    async def fake_list(*args, **kwargs):
        yield running_wf

    client.list_workflows = fake_list
    client.get_workflow_handle.return_value.terminate = AsyncMock()

    with patch("app.reconciler._terminate_workflow", new_callable=AsyncMock) as term:
        term.return_value = True
        count = await _cleanup_orphan_plan_children(client, "task-1")
    assert count == 1


@pytest.mark.asyncio
async def test_count_stuck_created_task_workflows():
    wf = MagicMock()
    wf.id = "workflow-stuck-created"
    wf.status = MagicMock()

    async def fake_list(*args, **kwargs):
        yield wf

    client = MagicMock()
    client.list_workflows = fake_list

    class FakeTask:
        id = "stuck-created"
        status = "created"
        updated_at = datetime.utcnow() - timedelta(minutes=50)

    with patch("app.reconciler._get_temporal", new_callable=AsyncMock, return_value=client):
        with patch("app.reconciler.AsyncSessionLocal") as mock_session:
            db = AsyncMock()
            mock_session.return_value.__aenter__.return_value = db
            result = MagicMock()
            result.scalar_one_or_none.return_value = FakeTask()
            db.execute = AsyncMock(return_value=result)
            count = await count_stuck_running_workflows()
    assert count == 1


@pytest.mark.asyncio
async def test_count_stuck_running_workflows():
    wf = MagicMock()
    wf.id = "workflow-stuck-task"
    wf.status = MagicMock()

    async def fake_list(*args, **kwargs):
        yield wf

    client = MagicMock()
    client.list_workflows = fake_list

    class FakeTask:
        id = "stuck-task"
        status = "running"
        updated_at = datetime.utcnow() - timedelta(minutes=50)

    with patch("app.reconciler._get_temporal", new_callable=AsyncMock, return_value=client):
        with patch("app.reconciler.AsyncSessionLocal") as mock_session:
            db = AsyncMock()
            mock_session.return_value.__aenter__.return_value = db
            result = MagicMock()
            result.scalar_one_or_none.return_value = FakeTask()
            db.execute = AsyncMock(return_value=result)
            count = await count_stuck_running_workflows()
    assert count == 1


@pytest.mark.asyncio
async def test_janitor_terminates_old_orphan():
    import importlib.util
    from datetime import timezone

    from app.config import RMP_ROOT

    janitor_path = os.path.join(RMP_ROOT, "ops", "workflow_janitor.py")
    spec = importlib.util.spec_from_file_location(
        "workflow_janitor", janitor_path
    )
    janitor_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(janitor_mod)

    wf = MagicMock()
    wf.id = "workflow-missing-task"
    wf.start_time = datetime.now(timezone.utc) - timedelta(hours=30)

    async def fake_list(*args, **kwargs):
        yield wf

    client = MagicMock()
    client.list_workflows = fake_list
    client.get_workflow_handle.return_value.terminate = AsyncMock()

    with patch.object(janitor_mod, "Client") as mock_client_cls:
        mock_client_cls.connect = AsyncMock(return_value=client)
        with patch.object(janitor_mod, "AsyncSessionLocal") as mock_session:
            db = AsyncMock()
            mock_session.return_value.__aenter__.return_value = db
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            db.execute = AsyncMock(return_value=result)
            stats = await janitor_mod.janitor_once(max_age_hours=24)
    assert stats["terminated"] == 1
