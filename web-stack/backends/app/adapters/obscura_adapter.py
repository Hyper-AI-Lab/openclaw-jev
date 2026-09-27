"""Obscura CDP browse — Hermes-compatible OBSCURA_CDP_URL wiring.

See https://github.com/SGavrl/hermes-plugin-obscura
Remote/Docker mode: OBSCURA_CDP_URL=http://127.0.0.1:9222
(polls /json/version for webSocketDebuggerUrl, then Playwright CDP connect).
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from app.adapters.crawl4ai_adapter import _http_markdown


def _normalize_http_base(cdp: str) -> str:
    """Turn ws(s)://host:port/... or http(s)://host:port into http://host:port."""
    raw = cdp.strip().rstrip("/")
    if raw.startswith("ws://"):
        raw = "http://" + raw[len("ws://") :]
    elif raw.startswith("wss://"):
        raw = "https://" + raw[len("wss://") :]
    parsed = urlparse(raw)
    if not parsed.scheme:
        raw = "http://" + raw
        parsed = urlparse(raw)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _resolve_ws_debugger_url(cdp: str, timeout: float = 15.0) -> Dict[str, Any]:
    base = _normalize_http_base(cdp)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{base}/json/version")
        resp.raise_for_status()
        data = resp.json()
    ws = data.get("webSocketDebuggerUrl") or data.get("webSocketUrl")
    if not ws and cdp.startswith("ws"):
        ws = cdp
    if not ws:
        raise RuntimeError(f"Obscura /json/version missing webSocketDebuggerUrl: {data}")
    return {"http_base": base, "ws": ws, "version": data}


async def _playwright_act(
    ws: str,
    url: str,
    *,
    action: str,
    selector: str = "",
    text: str = "",
    key: str = "",
) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(ws)
        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            act = action or "fetch"
            if act in ("fetch", "goto", "read", ""):
                pass
            elif act == "click":
                if not selector:
                    return {"ok": False, "error": "click requires selector", "action": act}
                await page.click(selector, timeout=15_000)
            elif act == "type":
                if not selector:
                    return {"ok": False, "error": "type requires selector", "action": act}
                await page.fill(selector, text or "")
            elif act == "press":
                await page.press(selector or "body", key or text or "Enter")
            else:
                return {"ok": False, "error": f"unknown action {act}", "action": act}
            title = await page.title()
            body = await page.inner_text("body")
            return {
                "ok": True,
                "backend": "obscura",
                "url": page.url,
                "title": title,
                "markdown": (body or "")[:120_000],
                "action": act,
                "cdp_ws": ws,
            }
        finally:
            await browser.close()


async def obscura_browse(
    url: str,
    *,
    action: str = "fetch",
    selector: str = "",
    text: str = "",
    key: str = "",
) -> Dict[str, Any]:
    act = action or "fetch"
    interactive = act in ("click", "type", "press")
    cdp = (os.environ.get("OBSCURA_CDP_URL") or "").strip()
    if not cdp:
        if interactive:
            return {
                "ok": False,
                "backend": "obscura",
                "action": act,
                "error": "Obscura CDP is down; click/type/press are omitted",
            }
        page = await _http_markdown(url)
        page["backend"] = "obscura_degraded_http"
        page["warning"] = (
            "Obscura CDP not configured (set OBSCURA_CDP_URL=http://127.0.0.1:9222); used HTTP fetch"
        )
        page["action"] = act
        return page

    try:
        resolved = await _resolve_ws_debugger_url(cdp)
        result = await _playwright_act(
            resolved["ws"],
            url,
            action=act,
            selector=selector,
            text=text,
            key=key,
        )
        if result.get("ok"):
            result["obscura_version"] = {
                k: resolved["version"].get(k)
                for k in ("Browser", "Protocol-Version", "User-Agent")
                if k in resolved["version"]
            }
        return result
    except Exception as exc:
        if interactive:
            return {
                "ok": False,
                "backend": "obscura",
                "action": act,
                "error": str(exc)[:400],
            }
        page = await _http_markdown(url)
        page["backend"] = "obscura_degraded_http"
        page["fallback_error"] = str(exc)[:400]
        page["cdp_url"] = cdp
        page["action"] = act
        return page
