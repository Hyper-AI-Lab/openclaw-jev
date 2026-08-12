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


async def _playwright_fetch(ws: str, url: str) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(ws)
        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            title = await page.title()
            text = await page.inner_text("body")
            html = await page.content()
            return {
                "ok": True,
                "backend": "obscura",
                "url": url,
                "title": title,
                "markdown": (text or "")[:120_000],
                "html_chars": len(html or ""),
                "cdp_ws": ws,
            }
        finally:
            # Remote Obscura owns lifecycle — disconnect only, do not kill server.
            await browser.close()


async def obscura_browse(url: str, *, action: str = "fetch") -> Dict[str, Any]:
    cdp = (os.environ.get("OBSCURA_CDP_URL") or "").strip()
    if not cdp:
        page = await _http_markdown(url)
        page["backend"] = "obscura_degraded_http"
        page["warning"] = (
            "Obscura CDP not configured (set OBSCURA_CDP_URL=http://127.0.0.1:9222); used HTTP fetch"
        )
        page["action"] = action
        return page

    try:
        resolved = await _resolve_ws_debugger_url(cdp)
        if action in ("fetch", "goto", "read", ""):
            result = await _playwright_fetch(resolved["ws"], url)
            result["action"] = action or "fetch"
            result["obscura_version"] = {
                k: resolved["version"].get(k)
                for k in ("Browser", "Protocol-Version", "User-Agent")
                if k in resolved["version"]
            }
            return result
        return {
            "ok": True,
            "backend": "obscura",
            "url": url,
            "action": action,
            "cdp_ws": resolved["ws"],
            "note": f"Connected to Obscura; action={action} not implemented beyond fetch — use OpenClaw browser for clicks",
        }
    except Exception as exc:
        page = await _http_markdown(url)
        page["backend"] = "obscura_degraded_http"
        page["fallback_error"] = str(exc)[:400]
        page["cdp_url"] = cdp
        return page
