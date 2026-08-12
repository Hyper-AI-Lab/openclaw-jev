"""Lightweight bulk crawler (Crawlee-style queues/retries)."""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Dict, List, Set
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

_JOBS: Dict[str, Dict[str, Any]] = {}


def _same_host(a: str, b: str) -> bool:
    return urlparse(a).netloc == urlparse(b).netloc


async def crawlee_start(
    start_url: str,
    *,
    max_pages: int = 10,
    max_depth: int = 2,
) -> Dict[str, Any]:
    import uuid

    job_id = str(uuid.uuid4())
    _JOBS[job_id] = {
        "id": job_id,
        "status": "running",
        "start_url": start_url,
        "max_pages": max_pages,
        "max_depth": max_depth,
        "pages": [],
        "errors": [],
    }
    asyncio.create_task(_run_job(job_id, start_url, max_pages, max_depth))
    return {"ok": True, "job_id": job_id, "status": "running"}


async def crawlee_status(job_id: str) -> Dict[str, Any]:
    job = _JOBS.get(job_id)
    if not job:
        return {"ok": False, "error": "job_not_found"}
    return {"ok": True, **job}


async def _run_job(job_id: str, start_url: str, max_pages: int, max_depth: int) -> None:
    job = _JOBS[job_id]
    seen: Set[str] = set()
    q: deque = deque([(start_url, 0)])
    headers = {"User-Agent": "AuraWebStack-Crawlee/1.0"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0, headers=headers) as client:
            while q and len(job["pages"]) < max_pages:
                url, depth = q.popleft()
                if url in seen:
                    continue
                seen.add(url)
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    soup = BeautifulSoup(resp.text, "lxml")
                    for tag in soup(["script", "style", "noscript"]):
                        tag.decompose()
                    title = soup.title.string.strip() if soup.title and soup.title.string else ""
                    text = soup.get_text("\n", strip=True)[:40_000]
                    job["pages"].append(
                        {"url": url, "title": title, "markdown": text, "depth": depth}
                    )
                    if depth < max_depth:
                        for a in soup.find_all("a", href=True):
                            href = urljoin(url, a["href"]).split("#")[0]
                            if href.startswith("http") and _same_host(start_url, href) and href not in seen:
                                q.append((href, depth + 1))
                except Exception as exc:
                    job["errors"].append({"url": url, "error": str(exc)[:200]})
                    if len(job["errors"]) > 50:
                        break
        job["status"] = "completed"
    except Exception as exc:
        job["status"] = "failed"
        job["errors"].append({"url": start_url, "error": str(exc)[:300]})
