"""Crawl4AI adapter + bounded httpx crawl."""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Set
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

_PAGE_CAP = 8
_DEPTH_CAP = 2


def _same_host(a: str, b: str) -> bool:
    return urlparse(a).netloc == urlparse(b).netloc


async def _fetch_page(url: str, timeout: float = 20.0) -> Dict[str, Any]:
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
    links = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(url, anchor["href"]).split("#")[0]
        if href.startswith("http"):
            links.append(href)
    return {
        "ok": True,
        "url": url,
        "title": title,
        "markdown": text[:40_000],
        "links": links[:50],
    }


async def _http_markdown(url: str, timeout: float = 45.0) -> Dict[str, Any]:
    page = await _fetch_page(url, timeout=timeout)
    page["backend"] = "http_fetch"
    page["markdown"] = (page.get("markdown") or "")[:120_000]
    return page


async def _bounded_crawl(url: str, *, max_pages: int, max_depth: int) -> Dict[str, Any]:
    pages: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    queue: deque = deque([(url, 0)])
    while queue and len(pages) < max_pages:
        current, depth = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        try:
            page = await _fetch_page(current)
        except Exception as exc:
            pages.append({"ok": False, "url": current, "error": str(exc)[:200], "depth": depth})
            continue
        page["depth"] = depth
        pages.append(page)
        if depth < max_depth:
            for link in page.get("links") or []:
                if _same_host(url, link) and link not in seen:
                    queue.append((link, depth + 1))
    first = pages[0] if pages else {"markdown": "", "title": ""}
    return {
        "ok": True,
        "backend": "http_fetch_bounded",
        "url": url,
        "title": first.get("title") or "",
        "markdown": (first.get("markdown") or "")[:120_000],
        "pages": pages,
        "pages_fetched": len(pages),
        "depth": max_depth,
        "max_pages": max_pages,
        "durable": False,
    }


async def crawl4ai_scrape(
    url: str,
    *,
    depth: int = 0,
    max_pages: int = 1,
) -> Dict[str, Any]:
    pages = max(1, min(int(max_pages or 1), _PAGE_CAP))
    deep = max(0, min(int(depth or 0), _DEPTH_CAP))
    if pages > 1 or deep > 0:
        return await _bounded_crawl(url, max_pages=pages, max_depth=max(deep, 1 if pages > 1 else deep))
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
            "depth": 0,
            "max_pages": 1,
            "pages_fetched": 1,
        }
    except Exception as exc:
        fallback = await _http_markdown(url)
        fallback["fallback_from"] = "crawl4ai"
        fallback["fallback_error"] = str(exc)[:300]
        fallback["pages_fetched"] = 1
        fallback["max_pages"] = 1
        return fallback
