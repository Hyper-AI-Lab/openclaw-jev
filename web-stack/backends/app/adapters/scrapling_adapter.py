"""Scrapling adaptive fetch with httpx fallback."""
from __future__ import annotations

from typing import Any, Dict

from app.adapters.crawl4ai_adapter import _http_markdown


async def scrapling_fetch(url: str, *, css: str | None = None) -> Dict[str, Any]:
    try:
        # scrapling API varies by version; try common entry points
        try:
            from scrapling import Fetcher

            page = Fetcher.get(url)
            html = getattr(page, "html_content", None) or str(page)
            text = getattr(page, "body", None) or html
            if css and hasattr(page, "css"):
                matched = page.css(css)
                text = str(matched)
            return {
                "ok": True,
                "backend": "scrapling",
                "url": url,
                "markdown": str(text)[:120_000],
                "css": css,
            }
        except Exception:
            from scrapling.fetchers import Fetcher

            page = Fetcher.get(url)
            text = getattr(page, "html_content", None) or str(page)
            return {
                "ok": True,
                "backend": "scrapling",
                "url": url,
                "markdown": str(text)[:120_000],
                "css": css,
            }
    except Exception as exc:
        fallback = await _http_markdown(url)
        fallback["fallback_from"] = "scrapling"
        fallback["fallback_error"] = str(exc)[:300]
        return fallback
