"""ScrapeGraph extraction. Missing LLM key is a failure, not a null schema."""
from __future__ import annotations

import os
from typing import Any, Dict


async def scrapegraph_extract(
    url: str,
    *,
    prompt: str,
    schema: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    api_key = (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("NVIDIA_API_KEY")
        or os.environ.get("SCRAPEGRAPH_LLM_KEY")
        or ""
    ).strip()
    if not api_key:
        return {
            "ok": False,
            "backend": "scrapegraphai",
            "url": url,
            "error": "OPENAI_API_KEY or NVIDIA_API_KEY missing",
        }
    try:
        from scrapegraphai.graphs import SmartScraperGraph

        graph_config = {
            "llm": {
                "api_key": api_key,
                "model": os.environ.get("SCRAPEGRAPH_MODEL", "openai/gpt-4o-mini"),
            },
            "verbose": False,
        }
        graph = SmartScraperGraph(prompt=prompt, source=url, config=graph_config)
        result = graph.run()
        return {
            "ok": True,
            "backend": "scrapegraphai",
            "url": url,
            "prompt": prompt,
            "schema": schema,
            "result": result,
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "scrapegraphai",
            "url": url,
            "error": str(exc)[:400],
        }
