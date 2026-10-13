"""Concurrent memory scope reads + shared NVIDIA embed cache."""
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_read_ordered_preserves_merge_priority(monkeypatch):
    import asyncio

    from app.memory.router import MemoryRouter

    async def fake_read(
        scope_type, scope_id, memory_type=None, limit=20, query=None, **kwargs
    ):
        await asyncio.sleep(0.05)
        label = memory_type or "any"
        return [
            {
                "id": f"{scope_type}-{label}",
                "memory_type": label,
                "content": f"{scope_type}:{label}",
                "source": "postgres",
                "confidence": 100,
            }
        ]

    monkeypatch.setattr(MemoryRouter, "read", staticmethod(fake_read))
    monkeypatch.setattr("app.memory.graph.query_links", AsyncMock(return_value=[]))

    items = await MemoryRouter.read_ordered(
        process_run_id="pr-1",
        process_type="login",
        task_id="t-1",
        limit=10,
    )
    contents = [i["content"] for i in items]
    # Task-scope memory has no writer, so it is no longer read.
    assert contents == [
        "process:working", "process:episodic", "procedural:procedural", "user:semantic", "user:pinned",
    ]


@pytest.mark.asyncio
async def test_read_ordered_runs_scopes_concurrently(monkeypatch):
    import asyncio

    from app.memory import router
    from app.memory.router import MemoryRouter

    # Five scopes (test_read_ordered_preserves_merge_priority), as many read at once as the cap allows.
    batch = min(router.MEMORY_READ_CONCURRENCY, 5)
    reading, most = 0, 0
    full = asyncio.Event()

    async def read_together(
        scope_type, scope_id, memory_type=None, limit=20, query=None, **kwargs
    ):
        # A read ends only once a full batch of reads has run at the same time, and it yields once more before it
        # ends, so reads past the cap would start meanwhile; read one by one, each times out.
        nonlocal reading, most
        reading += 1
        most = max(most, reading)
        if reading == batch:
            full.set()
        try:
            await asyncio.wait_for(full.wait(), 10)
            await asyncio.sleep(0)
        finally:
            reading -= 1
        return []

    monkeypatch.setattr(MemoryRouter, "read", staticmethod(read_together))
    monkeypatch.setattr("app.memory.graph.query_links", AsyncMock(return_value=[]))

    await MemoryRouter.read_ordered(
        process_run_id="pr-1",
        process_type="generic",
        task_id="t-1",
    )
    # As many at once as the cap allows, which is more than one, and never more.
    assert batch > 1 and most == batch


def test_nvidia_embed_cache_singleflight():
    from app.memory.nvidia_embed import NvidiaEmbeddings, clear_embed_cache

    clear_embed_cache()
    emb = NvidiaEmbeddings(api_key="test-key", model="nvidia/nv-embed-v1")
    calls = {"n": 0}

    class FakeData:
        embedding = [0.1, 0.2, 0.3]

    class FakeResp:
        data = [FakeData()]
        usage = None

    def fake_create(**kwargs):
        calls["n"] += 1
        time.sleep(0.05)
        return FakeResp()

    fake_client = MagicMock()
    fake_client.embeddings.create.side_effect = fake_create
    emb.client = fake_client

    with patch(
        "app.memory.nvidia_embed.wait_for_dispatch_sync", return_value="nvidia:default"
    ):
        with patch("app.memory.nvidia_embed.record_success"):
            with patch("app.memory.nvidia_embed.record_request"):
                with patch(
                    "app.memory.nvidia_embed.api_key_for_profile",
                    return_value="test-key",
                ):
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        futures = [
                            pool.submit(emb.embed_query, "same query text")
                            for _ in range(4)
                        ]
                        results = [f.result() for f in futures]

    assert calls["n"] == 1
    assert all(r == [0.1, 0.2, 0.3] for r in results)
    clear_embed_cache()
