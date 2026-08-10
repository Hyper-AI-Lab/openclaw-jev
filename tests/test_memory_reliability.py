"""Phase 3 memory reliability tests (R6)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.activities.db_activities import build_process_memory_context, write_process_memory
from app.memory.router import MemoryRouter, VECTOR_SEARCH_TIMEOUT_SEC, _vector_search_bounded


@pytest.mark.asyncio
async def test_vector_search_timeout_fail_soft():
    svc = MagicMock()

    async def slow_search(*args, **kwargs):
        await asyncio.sleep(VECTOR_SEARCH_TIMEOUT_SEC + 5)
        return [{"content": "never", "source": "vector"}]

    with patch("app.memory.router.asyncio.to_thread", side_effect=slow_search):
        hits = await _vector_search_bounded(svc, "process", "run-1", "query", 5, None)
    assert hits == []


@pytest.mark.asyncio
async def test_write_process_memory_provenance_kwarg():
    with patch("app.memory.router.MemoryRouter.write", new_callable=AsyncMock) as mock_write:
        mock_write.return_value = "mem-id"
        await write_process_memory(
            {
                "scope_id": "run-1",
                "content": "hello",
                "provenance": {"task_id": "t1"},
            }
        )
        mock_write.assert_awaited_once()
        kwargs = mock_write.call_args.kwargs
        assert kwargs["provenance"] == {"task_id": "t1"}


@pytest.mark.asyncio
async def test_build_context_skip_vector():
    with patch(
        "app.memory.router.MemoryRouter.build_context_block", new_callable=AsyncMock
    ) as mock_build:
        mock_build.return_value = "BLOCK"
        await build_process_memory_context(
            {"process_run_id": "run-1", "skip_vector": True, "semantic_query": "x"}
        )
        mock_build.assert_awaited_once()
        kwargs = mock_build.call_args.kwargs
        assert kwargs["skip_vector"] is True
        assert kwargs["query"] is None


@pytest.mark.asyncio
async def test_read_postgres_only_when_skip_vector():
    with patch("app.memory.router.AsyncSessionLocal") as mock_session:
        db = AsyncMock()
        mock_session.return_value.__aenter__.return_value = db
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        with patch("app.memory.router.is_vector_memory_enabled", return_value=True):
            with patch("app.memory.router.get_vector_service") as mock_svc:
                items = await MemoryRouter.read(
                    "process", "run-1", query="test query", skip_vector=True
                )
                mock_svc.assert_not_called()
        assert items == []
