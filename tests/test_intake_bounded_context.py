"""Bounded intake context — vector leg deadline + concurrent retrieval."""
import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest


def _slow_vector_search(*args, **kwargs):
    time.sleep(2)
    return [{"task_id": "slow", "score": 0.9}]


@pytest.mark.asyncio
async def test_hybrid_search_bounded_skips_vector_on_timeout():
    from app.task_registry.retriever import hybrid_search_bounded

    with patch(
        "app.task_registry.retriever.fetch_active_tasks",
        new=AsyncMock(return_value=[{"task_id": "active-1"}]),
    ):
        with patch(
            "app.task_registry.retriever.fetch_recent_registry",
            new=AsyncMock(return_value=[]),
        ):
            with patch(
                "app.task_registry.retriever.search_similar_tasks",
                side_effect=_slow_vector_search,
            ):
                result = await hybrid_search_bounded(
                    "hello",
                    deadline_sec=0.05,
                )

    assert result["active_tasks"] == [{"task_id": "active-1"}]
    assert result["vector_similar"] == []


@pytest.mark.asyncio
async def test_hybrid_search_runs_legs_concurrently():
    from app.task_registry.retriever import hybrid_search_bounded

    async def slow_active(**kwargs):
        await asyncio.sleep(0.15)
        return [{"task_id": "a1"}]

    async def slow_recent(**kwargs):
        await asyncio.sleep(0.15)
        return [{"task_id": "r1", "task_ended_at": None}]

    def slow_vector(*args, **kwargs):
        time.sleep(0.15)
        return [{"task_id": "v1", "score": 0.9}]

    with patch(
        "app.task_registry.retriever.fetch_active_tasks",
        new=AsyncMock(side_effect=slow_active),
    ):
        with patch(
            "app.task_registry.retriever.fetch_recent_registry",
            new=AsyncMock(side_effect=slow_recent),
        ):
            with patch(
                "app.task_registry.retriever.search_similar_tasks",
                side_effect=slow_vector,
            ):
                started = time.monotonic()
                result = await hybrid_search_bounded("hello", deadline_sec=2.0)
                elapsed = time.monotonic() - started

    assert result["active_tasks"][0]["task_id"] == "a1"
    assert result["recent_registry"][0]["task_id"] == "r1"
    assert result["vector_similar"][0]["task_id"] == "v1"
    # Serial would be ~0.45s; concurrent should finish near the slowest leg.
    assert elapsed < 0.55


@pytest.mark.asyncio
async def test_assemble_intake_context_loads_messages_concurrently():
    from app.task_registry.intake_context import assemble_intake_context

    class Msg:
        def __init__(self, role, content, source):
            self.role = role
            self.content = content
            self.source = source

    async def slow_messages(tid, limit=5):
        await asyncio.sleep(0.12)
        return [Msg("user", f"msg-{tid}", "slack")]

    retrieval = {
        "active_tasks": [{"task_id": "t1"}],
        "recent_registry": [{"task_id": "t2"}],
        "vector_similar": [{"task_id": "t3"}],
    }

    with patch(
        "app.task_registry.intake_context.hybrid_search_bounded",
        new=AsyncMock(return_value=retrieval),
    ), patch(
        "app.task_registry.intake_context.recent_session_dialogue_block",
        new=AsyncMock(return_value=""),
    ):
        with patch(
            "app.task_registry.intake_context.list_task_messages",
            new=AsyncMock(side_effect=slow_messages),
        ):
            started = time.monotonic()
            result = await assemble_intake_context("hello", session_key="s1")
            elapsed = time.monotonic() - started

    assert set(result["supplementary_messages"]) == {"t1", "t2", "t3"}
    assert elapsed < 0.30


@pytest.mark.asyncio
async def test_intake_reads_the_dialogue_beside_retrieval_once():
    from app.task_registry.intake_context import assemble_intake_context

    async def slow_retrieval(*args, **kwargs):
        await asyncio.sleep(0.15)
        return {"active_tasks": [], "recent_registry": [], "vector_similar": []}

    async def slow_dialogue(session_key):
        await asyncio.sleep(0.15)
        return "RECENT DIALOGUE (same Slack session — continue this conversation):\n[21:03] Kirill: hi\n[21:04] Aura: Hello!"

    with patch("app.task_registry.intake_context.hybrid_search_bounded", new=AsyncMock(side_effect=slow_retrieval)), \
         patch("app.task_registry.intake_context.recent_session_dialogue_block", new=AsyncMock(side_effect=slow_dialogue)) as dialogue:
        started = time.monotonic()
        result = await assemble_intake_context("Which one?", session_key="s1")
        elapsed = time.monotonic() - started

    assert result["recent_dialogue"] == ["[21:03] Kirill: hi", "[21:04] Aura: Hello!"]
    dialogue.assert_awaited_once_with("s1")
    assert elapsed < 0.28


@pytest.mark.asyncio
async def test_assemble_intake_context_hides_canary_from_user_dm():
    from app.task_registry.intake_context import assemble_intake_context

    retrieval = {
        "active_tasks": [
            {
                "task_id": "canary-1",
                "task_type": "canary",
                "goal": "RMP CANARY: Reply with exactly CANARY_OK",
            },
            {
                "task_id": "user-1",
                "task_type": "user",
                "goal": "Summarize the weekly report",
            },
        ],
        "recent_registry": [],
        "vector_similar": [],
        "evidence_pack": [],
    }

    with patch(
        "app.task_registry.intake_context.hybrid_search_bounded",
        new=AsyncMock(return_value=retrieval),
    ):
        with patch(
            "app.task_registry.intake_context.list_task_messages",
            new=AsyncMock(return_value=[]),
        ):
            result = await assemble_intake_context(
                "Are you here aura?",
                session_key="agent:main:main",
                tags=["user-request"],
            )

    ids = [t["task_id"] for t in result["active_tasks"]]
    assert ids == ["user-1"]
