"""Obscura click uses Playwright when CDP is configured."""
import asyncio
import sys

import pytest

pytest.importorskip("bs4")
sys.path.insert(0, "/root/.openclaw/web-stack/backends")

from app.adapters import obscura_adapter as ob


def test_click_uses_playwright_when_cdp_set(monkeypatch):
    seen = {}

    async def fake_resolve(cdp, timeout=15.0):
        seen["cdp"] = cdp
        return {"ws": "ws://127.0.0.1:9222/devtools/browser", "version": {}}

    async def fake_act(ws, url, *, action, selector, text, key):
        seen.update({"ws": ws, "url": url, "action": action, "selector": selector})
        return {"ok": True, "backend": "obscura", "action": action, "markdown": "clicked"}

    monkeypatch.setenv("OBSCURA_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setattr(ob, "_resolve_ws_debugger_url", fake_resolve)
    monkeypatch.setattr(ob, "_playwright_act", fake_act)
    out = asyncio.run(
        ob.obscura_browse("https://example.com", action="click", selector="#go")
    )
    assert out["ok"] is True
    assert seen["action"] == "click"
    assert seen["selector"] == "#go"


def test_click_without_cdp_is_not_success(monkeypatch):
    monkeypatch.delenv("OBSCURA_CDP_URL", raising=False)
    out = asyncio.run(ob.obscura_browse("https://example.com", action="click", selector="#go"))
    assert out["ok"] is False
    assert "CDP" in out["error"]
