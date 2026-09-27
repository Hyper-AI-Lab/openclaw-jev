"""Bounded crawl honors max_pages."""
import asyncio
import sys

import pytest

pytest.importorskip("bs4")
sys.path.insert(0, "/root/.openclaw/web-stack/backends")

from app.adapters import crawl4ai_adapter as crawl
from app.adapters.status import probe_backends


def test_max_pages_limits_fetches(monkeypatch):
    calls = []

    async def fake_fetch(url, timeout=20.0):
        calls.append(url)
        nxt = f"https://example.com/p{len(calls)}"
        return {
            "ok": True,
            "url": url,
            "title": "t",
            "markdown": "body",
            "links": [nxt, "https://other.example/x"],
        }

    monkeypatch.setattr(crawl, "_fetch_page", fake_fetch)
    out = asyncio.run(crawl.crawl4ai_scrape("https://example.com/start", depth=2, max_pages=2))
    assert out["pages_fetched"] == 2
    assert len(calls) == 2
    assert out["backend"] == "http_fetch_bounded"
    assert out["durable"] is False
    assert all("other.example" not in url for url in calls)


def test_crawlee_status_is_in_memory_python():
    status = probe_backends()["crawlee"]
    assert status["engine"] == "python_bfs"
    assert status["durable"] is False
    assert "node_crawlee" not in status
    assert "lost" in status["detail"]
