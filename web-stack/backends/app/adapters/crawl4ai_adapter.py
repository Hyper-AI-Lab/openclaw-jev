"""Crawl4AI adapter + httpx fallback."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup


async def _http_markdown(url: str, timeout: float = 45.0) -> Dict[str, Any]:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": "AuraWebStack/1.0 (+https://github.com/Hyper-AI-Lab/aura)"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    return {
        "ok": True,
        "backend": "http_fetch",
        "url": url,
        "title": title,
        "markdown": text[:120_000],
        "links": [a.get("href") for a in soup.find_all("a", href=True)][:200],
    }


async def crawl4ai_scrape(
    url: str,
    *,
    depth: int = 0,
    max_pages: int = 1,
) -> Dict[str, Any]:
    try:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig

        config = CrawlerRunConfig()
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url, config=config)
        md = getattr(result, "markdown", None) or getattr(result, "cleaned_html", "") or ""
        if hasattr(md, "raw_markdown"):
            md = md.raw_markdown
        return {
            "ok": True,
            "backend": "crawl4ai",
            "url": url,
            "markdown": str(md)[:120_000],
            "success": bool(getattr(result, "success", True)),
            "links": list(getattr(result, "links", {}) or {})[:200]
            if isinstance(getattr(result, "links", None), dict)
            else [],
            "depth": depth,
            "max_pages": max_pages,
        }
    except Exception as exc:
        fallback = await _http_markdown(url)
        fallback["fallback_from"] = "crawl4ai"
        fallback["fallback_error"] = str(exc)[:300]
        return fallback
