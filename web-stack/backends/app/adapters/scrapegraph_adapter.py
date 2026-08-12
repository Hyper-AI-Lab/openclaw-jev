"""ScrapeGraphAI-style schema extraction with LLM-optional fallback."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict

from app.adapters.crawl4ai_adapter import _http_markdown


async def scrapegraph_extract(
    url: str,
    *,
    prompt: str,
    schema: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    page = await _http_markdown(url)
    text = page.get("markdown") or ""

    # Try scrapegraphai if installed
    try:
        from scrapegraphai.graphs import SmartScraperGraph

        api_key = (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("NVIDIA_API_KEY")
            or os.environ.get("SCRAPEGRAPH_LLM_KEY")
            or ""
        )
        graph_config = {
            "llm": {
                "api_key": api_key,
                "model": os.environ.get("SCRAPEGRAPH_MODEL", "openai/gpt-4o-mini"),
            },
            "verbose": False,
        }
        if api_key:
            graph = SmartScraperGraph(prompt=prompt, source=url, config=graph_config)
            result = graph.run()
            return {
                "ok": True,
                "backend": "scrapegraphai",
                "url": url,
                "prompt": prompt,
                "result": result,
            }
    except Exception as exc:
        scrape_err = str(exc)[:300]
    else:
        scrape_err = "no_api_key_or_skipped"

    # Heuristic fallback: return page text + requested schema keys as empty stubs
    fields = list((schema or {}).keys()) if schema else _guess_fields(prompt)
    stub = {f: None for f in fields}
    stub["_excerpt"] = text[:4000]
    stub["_note"] = (
        "ScrapeGraphAI unavailable or no LLM key; returned excerpt for agent extraction. "
        f"detail={scrape_err}"
    )
    return {
        "ok": True,
        "backend": "heuristic_extract",
        "url": url,
        "prompt": prompt,
        "schema": schema,
        "result": stub,
    }


def _guess_fields(prompt: str) -> list:
    # crude: quoted words or "field X" patterns
    quoted = re.findall(r"['\"]([a-zA-Z0-9_]+)['\"]", prompt)
    if quoted:
        return quoted[:20]
    words = re.findall(r"\b([a-z][a-z0-9_]{2,})\b", prompt.lower())
    skip = {"the", "and", "from", "this", "site", "page", "extract", "pull", "these", "fields"}
    return [w for w in words if w not in skip][:12] or ["data"]
