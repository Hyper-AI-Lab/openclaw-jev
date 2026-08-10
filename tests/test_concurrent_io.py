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
    assert contents[0] == "process:working"
    assert contents[1] == "process:episodic"
    assert contents[2] == "task:any"
    assert "procedural:procedural" in contents
    assert "user:semantic" in contents
    assert "user:pinned" in contents


@pytest.mark.asyncio
async def test_read_ordered_runs_scopes_concurrently(monkeypatch):
    import asyncio

    from app.memory.router import MemoryRouter

    async def slow_read(
        scope_type, scope_id, memory_type=None, limit=20, query=None, **kwargs
    ):
        await asyncio.sleep(0.12)
        return []

    monkeypatch.setattr(MemoryRouter, "read", staticmethod(slow_read))
    monkeypatch.setattr("app.memory.graph.query_links", AsyncMock(return_value=[]))

    started = time.monotonic()
    await MemoryRouter.read_ordered(
        process_run_id="pr-1",
        process_type="generic",
        task_id="t-1",
    )
    elapsed = time.monotonic() - started
    # 6 scopes * 0.12s serial ~= 0.72s; with concurrency=3 expect ~0.24–0.40s.
    assert elapsed < 0.55


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
