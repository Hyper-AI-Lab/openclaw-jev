"""Aura web-stack FastAPI gateway — localhost-only backends for OpenClaw tools."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.adapters.browser_use_adapter import browser_use_run
from app.adapters.crawl4ai_adapter import crawl4ai_scrape
from app.adapters.crawlee_adapter import crawlee_start, crawlee_status
from app.adapters.obscura_adapter import obscura_browse
from app.adapters.scrapegraph_adapter import scrapegraph_extract
from app.adapters.scrapling_adapter import scrapling_fetch
from app.adapters.status import probe_backends

app = FastAPI(title="Aura Web Stack", version="1.0.0")


class Crawl4AIRequest(BaseModel):
    url: str
    depth: int = 0
    max_pages: int = 1


class ScraplingRequest(BaseModel):
    url: str
    css: Optional[str] = None


class CrawleeRequest(BaseModel):
    url: str
    max_pages: int = Field(default=10, ge=1, le=100)
    max_depth: int = Field(default=2, ge=0, le=5)


class ScrapeGraphRequest(BaseModel):
    url: str
    prompt: str
    extract_schema: Optional[Dict[str, Any]] = Field(default=None, alias="schema")

    model_config = {"populate_by_name": True}


class BrowserUseRequest(BaseModel):
    task: str
    start_url: Optional[str] = None


class ObscuraRequest(BaseModel):
    url: str
    action: str = "fetch"


@app.get("/health")
async def health() -> Dict[str, Any]:
    backends = probe_backends()
    available = sum(1 for v in backends.values() if v.get("available"))
    return {
        "ok": True,
        "service": "aura-web-stack",
        "backends": backends,
        "available_count": available,
        "total_count": len(backends),
    }


@app.post("/v1/crawl4ai")
async def v1_crawl4ai(body: Crawl4AIRequest) -> Dict[str, Any]:
    return await crawl4ai_scrape(body.url, depth=body.depth, max_pages=body.max_pages)


@app.post("/v1/scrapling")
async def v1_scrapling(body: ScraplingRequest) -> Dict[str, Any]:
    return await scrapling_fetch(body.url, css=body.css)


@app.post("/v1/crawlee")
async def v1_crawlee(body: CrawleeRequest) -> Dict[str, Any]:
    return await crawlee_start(body.url, max_pages=body.max_pages, max_depth=body.max_depth)


@app.get("/v1/crawlee/{job_id}")
async def v1_crawlee_status(job_id: str) -> Dict[str, Any]:
    result = await crawlee_status(job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error"))
    return result


@app.post("/v1/scrapegraph")
async def v1_scrapegraph(body: ScrapeGraphRequest) -> Dict[str, Any]:
    return await scrapegraph_extract(body.url, prompt=body.prompt, schema=body.extract_schema)


@app.post("/v1/browser-use")
async def v1_browser_use(body: BrowserUseRequest) -> Dict[str, Any]:
    return await browser_use_run(body.task, start_url=body.start_url)


@app.post("/v1/obscura")
async def v1_obscura(body: ObscuraRequest) -> Dict[str, Any]:
    return await obscura_browse(body.url, action=body.action)


ROUTING_CHEATSHEET = {
    "search": ["web_search (Brave)", "langsearch_search"],
    "fetch": ["jina_reader", "web_fetch", "crawl4ai"],
    "crawl": ["crawl4ai", "crawlee_crawl"],
    "adaptive_extract": ["scrapling", "crawl4ai"],
    "schema_extract": ["scrapegraph_extract", "scrapling"],
    "interact": ["browser", "browser_use", "obscura_browse"],
}


@app.get("/v1/routing")
async def v1_routing() -> Dict[str, Any]:
    return {"ok": True, "routing": ROUTING_CHEATSHEET, "backends": probe_backends()}
