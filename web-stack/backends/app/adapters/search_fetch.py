"""Search and reader routes the evaluator can call on localhost."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import httpx

_OPENCLAW_JSON = Path("/root/.openclaw/openclaw.json")
_LANGSEARCH_URL = "https://api.langsearch.com/v1/web-search"
_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_JINA_BASE = "https://r.jina.ai"


def _plugin_key(entry: str, *path: str) -> str:
    env_name = "LANGSEARCH_API_KEY" if entry == "langsearch" else "JINA_API_KEY"
    env = (os.environ.get(env_name) or "").strip()
    if env:
        return env
    try:
        cfg = json.loads(_OPENCLAW_JSON.read_text(encoding="utf-8"))
        node = (cfg.get("plugins") or {}).get("entries") or {}
        if entry == "langsearch":
            key = (((node.get("langsearch") or {}).get("config") or {}).get("webSearch") or {}).get("apiKey")
        else:
            cur: Any = (node.get("aura_web") or {}).get("config") or {}
            for part in path:
                cur = (cur or {}).get(part) or {}
            key = cur if isinstance(cur, str) else ""
        key = str(key or "").strip()
        if key and key != "<>":
            return key
    except Exception:
        return ""
    return ""


def web_search(query: str) -> Dict[str, Any]:
    q = (query or "").strip()[:200]
    if not q:
        return {"ok": False, "error": "empty query"}
    lang_key = _plugin_key("langsearch")
    if lang_key:
        try:
            response = httpx.post(
                _LANGSEARCH_URL,
                json={"query": q, "freshness": "noLimit", "summary": True, "count": 5},
                headers={"Authorization": f"Bearer {lang_key}"},
                timeout=20.0,
            )
        except Exception as exc:
            return {"ok": False, "error": f"langsearch unreachable: {exc}"[:300]}
        if response.status_code >= 400:
            return {"ok": False, "error": f"langsearch HTTP {response.status_code}"}
        data = response.json()
        pages = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
        results = [
            {
                "title": p.get("name") or p.get("title") or "",
                "url": p.get("url") or "",
                "snippet": p.get("snippet") or p.get("summary") or "",
            }
            for p in pages[:5]
        ]
        return {"ok": True, "provider": "langsearch", "query": q, "results": results}
    brave = (os.environ.get("BRAVE_API_KEY") or "").strip()
    if not brave:
        return {"ok": False, "error": "search key missing"}
    try:
        response = httpx.get(
            _BRAVE_URL,
            params={"q": q, "count": 5},
            headers={"X-Subscription-Token": brave, "Accept": "application/json"},
            timeout=20.0,
        )
    except Exception as exc:
        return {"ok": False, "error": f"brave unreachable: {exc}"[:300]}
    if response.status_code >= 400:
        return {"ok": False, "error": f"brave HTTP {response.status_code}"}
    data = response.json()
    web = ((data.get("web") or {}).get("results") or [])[:5]
    results = [
        {"title": p.get("title") or "", "url": p.get("url") or "", "snippet": p.get("description") or ""}
        for p in web
    ]
    return {"ok": True, "provider": "brave", "query": q, "results": results}


def jina_read(url: str) -> Dict[str, Any]:
    target = (url or "").strip()
    if not target.startswith("http://") and not target.startswith("https://"):
        return {"ok": False, "error": "url required"}
    headers = {"Accept": "text/plain"}
    key = _plugin_key("aura_web", "jina", "apiKey")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        response = httpx.get(f"{_JINA_BASE}/{target}", headers=headers, timeout=30.0, follow_redirects=True)
    except Exception as exc:
        return {"ok": False, "error": f"jina unreachable: {exc}"[:300]}
    if response.status_code >= 400:
        return {"ok": False, "error": f"jina HTTP {response.status_code}"}
    return {"ok": True, "provider": "jina", "url": target, "markdown": (response.text or "")[:8000]}
