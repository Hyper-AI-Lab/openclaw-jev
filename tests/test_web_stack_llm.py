"""Missing LLM key must not look like a successful extract."""
import asyncio
import sys

import pytest

pytest.importorskip("bs4")
sys.path.insert(0, "/root/.openclaw/web-stack/backends")

from app.adapters.scrapegraph_adapter import scrapegraph_extract


def test_scrapegraph_missing_key_is_not_ok(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("SCRAPEGRAPH_LLM_KEY", raising=False)
    out = asyncio.run(scrapegraph_extract("https://example.com", prompt="name and price"))
    assert out["ok"] is False
    assert "missing" in out["error"]
    assert out.get("result") is None
