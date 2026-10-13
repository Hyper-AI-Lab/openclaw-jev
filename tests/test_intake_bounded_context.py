"""Bounded intake context — vector leg deadline + concurrent retrieval."""
import asyncio
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest

# How long a leg waits for the others: far past any scheduling delay on a loaded host, and how long a run that awaits
# the legs one at a time takes to fail.
TOGETHER_SEC = 10.0


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

    # Each leg returns only once all three have started, so every leg starts before any finishes; legs awaited one
    # at a time never get past the first. The vector leg's thread runs under the search's 2 s deadline; it starts
    # after the other two, so it finds them started and does not wait.
    started, all_started = [], threading.Event()

    def start(leg):
        started.append(leg)
        if len(started) == 3:
            all_started.set()

    def wait_for_the_others():
        assert all_started.wait(TOGETHER_SEC), f"only {started} started"

    async def active(**kwargs):
        start("active")
        await asyncio.to_thread(wait_for_the_others)
        return [{"task_id": "a1"}]

    async def recent(**kwargs):
        start("recent")
        await asyncio.to_thread(wait_for_the_others)
        return [{"task_id": "r1", "task_ended_at": None}]

    def vector(*args, **kwargs):
        start("vector")
        wait_for_the_others()
        return [{"task_id": "v1", "score": 0.9}]

    with patch(
        "app.task_registry.retriever.fetch_active_tasks",
        new=AsyncMock(side_effect=active),
    ):
        with patch(
            "app.task_registry.retriever.fetch_recent_registry",
            new=AsyncMock(side_effect=recent),
        ):
            with patch(
                "app.task_registry.retriever.search_similar_tasks",
                side_effect=vector,
            ):
                result = await hybrid_search_bounded("hello", deadline_sec=2.0)

    assert result["active_tasks"][0]["task_id"] == "a1"
    assert result["recent_registry"][0]["task_id"] == "r1"
    assert result["vector_similar"][0]["task_id"] == "v1"


@pytest.mark.asyncio
async def test_assemble_intake_context_loads_messages_concurrently():
    from app.task_registry.intake_context import assemble_intake_context

    class Msg:
        def __init__(self, role, content, source):
            self.role = role
            self.content = content
            self.source = source

    # Each task's messages arrive only once all three loads are running.
    together = asyncio.Barrier(3)

    async def messages(tid, limit=5):
        await asyncio.wait_for(together.wait(), TOGETHER_SEC)
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
            new=AsyncMock(side_effect=messages),
        ):
            result = await assemble_intake_context("hello", session_key="s1")

    assert set(result["supplementary_messages"]) == {"t1", "t2", "t3"}


@pytest.mark.asyncio
async def test_intake_reads_the_dialogue_beside_retrieval_once():
    from app.task_registry.intake_context import assemble_intake_context

    # Retrieval and the dialogue each return only once both are running.
    together = asyncio.Barrier(2)

    async def retrieval(*args, **kwargs):
        await asyncio.wait_for(together.wait(), TOGETHER_SEC)
        return {"active_tasks": [], "recent_registry": [], "vector_similar": []}

    async def recent_dialogue(session_key):
        await asyncio.wait_for(together.wait(), TOGETHER_SEC)
        return "RECENT DIALOGUE (same Slack session — continue this conversation):\n[21:03] Kirill: hi\n[21:04] Aura: Hello!"

    with patch("app.task_registry.intake_context.hybrid_search_bounded", new=AsyncMock(side_effect=retrieval)), \
         patch("app.task_registry.intake_context.recent_session_dialogue_block", new=AsyncMock(side_effect=recent_dialogue)) as dialogue:
        result = await assemble_intake_context("Which one?", session_key="s1")

    assert result["recent_dialogue"] == ["[21:03] Kirill: hi", "[21:04] Aura: Hello!"]
    dialogue.assert_awaited_once_with("s1")


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
