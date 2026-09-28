"""POST /tasks/{id}/cancel is a terminal path and must index the task."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_cancel_indexes_the_task_after_commit():
    from app.api import server

    class FakeTask:
        id = "t-cancel"
        status = "running"
        correlation_id = "t-cancel"
        next_check_at = None

    task = FakeTask()
    task_row = MagicMock()
    task_row.scalar_one_or_none.return_value = task
    no_run = MagicMock()
    no_run.scalar_one_or_none.return_value = None

    order = MagicMock()
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[task_row, no_run])
    db.commit = AsyncMock(side_effect=lambda: order.commit())
    client = MagicMock()
    client.get_workflow_handle.return_value.signal = AsyncMock()
    client.get_workflow_handle.return_value.terminate = AsyncMock()
    index = AsyncMock(side_effect=lambda task_id: order.index(task_id))

    with patch.object(server, "connect_temporal", new_callable=AsyncMock, return_value=client), \
         patch("app.task_registry.hooks.index_terminal_task_async", index):
        out = await server.cancel_task("t-cancel", db=db)

    assert out == {"status": "cancelled", "task_id": "t-cancel"}
    assert task.status == "stopped_by_user"
    assert [c[0] for c in order.mock_calls] == ["commit", "index"]
    index.assert_awaited_once_with("t-cancel")
