"""Probe which optional backends are importable / configured."""
from __future__ import annotations

import os
from typing import Any, Dict

import httpx


def probe_backends() -> Dict[str, Any]:
    status: Dict[str, Any] = {}

    try:
        import crawl4ai  # noqa: F401

        status["crawl4ai"] = {"available": True, "detail": "import_ok"}
    except Exception as exc:
        status["crawl4ai"] = {"available": False, "detail": f"unavailable: {exc.__class__.__name__}"}

    try:
        import scrapling  # noqa: F401

        status["scrapling"] = {"available": True, "detail": "import_ok"}
    except Exception as exc:
        status["scrapling"] = {"available": False, "detail": f"unavailable: {exc.__class__.__name__}"}

    try:
        import scrapegraph_py  # noqa: F401

        status["scrapegraph"] = {"available": True, "detail": "import_ok"}
    except Exception:
        try:
            import scrapegraphai  # noqa: F401

            status["scrapegraph"] = {"available": True, "detail": "import_ok"}
        except Exception as exc:
            status["scrapegraph"] = {
                "available": False,
                "detail": f"unavailable: {exc.__class__.__name__}",
            }

    try:
        import browser_use  # noqa: F401

        status["browser_use"] = {"available": True, "detail": "import_ok"}
    except Exception as exc:
        status["browser_use"] = {
            "available": False,
            "detail": f"unavailable: {exc.__class__.__name__}",
        }

    status["crawlee"] = {
        "available": True,
        "detail": "python_bfs_crawler",
        "node_crawlee": os.path.exists("/usr/bin/npx"),
    }

    obscura_url = (os.environ.get("OBSCURA_CDP_URL") or "").strip()
    obscura: Dict[str, Any] = {
        "available": False,
        "detail": "set OBSCURA_CDP_URL when Obscura is running",
        "cdp_url": obscura_url or None,
    }
    if obscura_url:
        try:
            from app.adapters.obscura_adapter import _normalize_http_base

            base = _normalize_http_base(obscura_url)
            resp = httpx.get(f"{base}/json/version", timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                obscura = {
                    "available": True,
                    "detail": "cdp_ok",
                    "cdp_url": obscura_url,
                    "browser": data.get("Browser"),
                    "ws": data.get("webSocketDebuggerUrl"),
                }
            else:
                obscura["detail"] = f"cdp_http_{resp.status_code}"
        except Exception as exc:
            obscura["detail"] = f"cdp_unreachable: {exc.__class__.__name__}"
    status["obscura"] = obscura

    status["http_fetch"] = {"available": True, "detail": "httpx+bs4"}
    return status
