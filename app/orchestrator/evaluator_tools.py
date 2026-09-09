"""Bounded situational toolkit for the Process Evaluator (not Aura)."""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import httpx

logger = logging.getLogger("rmp.evaluator_tools")

ALLOWED_TOOLS = frozenset(
    {
        "health",
        "readiness",
        "web_capability_status",
        "web_search",
        "jina_reader",
    }
)
DENIED_OPS = frozenset(
    {
        "filesystem_write",
        "write_file",
        "systemctl",
        "plugin_install",
        "plugin_enable",
        "read_secrets",
        "openclaw.json",
        "rmp.env",
        "shell",
        "exec",
    }
)
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
_ALLOWED_PORTS = frozenset({8000, 8791})
_NEWSY = re.compile(
    r"\b(today|latest|current|news|weather|price|who won|as of)\b",
    re.IGNORECASE,
)


def tool_is_denied(name: str) -> bool:
    raw = (name or "").strip().lower()
    if raw in DENIED_OPS:
        return True
    if any(tok in raw for tok in ("systemctl", "/etc/", "secret", "passwd", ".env")):
        return True
    return False


def authorize_evaluator_tool(name: str) -> Optional[str]:
    """Return None if allowed, else a deny reason."""
    raw = (name or "").strip().lower()
    if tool_is_denied(raw):
        return f"denied privileged op: {raw}"
    if raw not in ALLOWED_TOOLS:
        return f"not in evaluator toolkit: {raw}"
    return None


def _needs_external_fact(intent: str) -> bool:
    return bool(_NEWSY.search(intent or ""))


def _intent_url(intent: str) -> str:
    urls = re.findall(r"https?://[^\s\)\]>\"']+", intent or "")
    return urls[0] if urls else ""


async def _local_get(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if host not in _ALLOWED_HOSTS or port not in _ALLOWED_PORTS:
        return {"ok": False, "denied": True, "error": f"url not allowlisted: {url}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers or {}, timeout=4.0)
        ctype = resp.headers.get("content-type") or ""
        if "json" in ctype:
            body: Any = resp.json()
        else:
            body = {"text": (resp.text or "")[:1500]}
        return {"ok": resp.status_code < 400, "status": resp.status_code, "body": body}


async def execute_evaluator_tool(name: str, *, intent: str = "") -> Dict[str, Any]:
    deny = authorize_evaluator_tool(name)
    if deny:
        return {"tool": name, "ok": False, "denied": True, "error": deny}
    try:
        if name == "health":
            return {"tool": "health", **(await _local_get("http://127.0.0.1:8000/health"))}
        if name == "readiness":
            from app.config import get_api_key

            headers = {}
            key = get_api_key()
            if key:
                headers["X-RMP-API-Key"] = key
            return {
                "tool": "readiness",
                **(
                    await _local_get(
                        "http://127.0.0.1:8000/api/production/readiness",
                        headers=headers,
                    )
                ),
            }
        if name == "web_capability_status":
            from app.orchestrator.web_capability import analyze_web_capability

            analysis = analyze_web_capability(intent or "")
            stack = await _local_get("http://127.0.0.1:8791/health")
            return {
                "tool": "web_capability_status",
                "ok": True,
                "analysis": {
                    "intent_class": analysis.get("intent_class"),
                    "preferred_tools": analysis.get("preferred_tools"),
                },
                "stack": stack,
            }
        if name == "web_search":
            q = quote((intent or "")[:200])
            return {
                "tool": "web_search",
                **(await _local_get(f"http://127.0.0.1:8791/v1/search?q={q}")),
            }
        if name == "jina_reader":
            target = _intent_url(intent)
            if not target:
                return {"tool": "jina_reader", "ok": False, "error": "no url in ask"}
            return {
                "tool": "jina_reader",
                **(
                    await _local_get(
                        f"http://127.0.0.1:8791/v1/jina?url={quote(target, safe='')}"
                    )
                ),
            }
    except Exception as exc:
        logger.info("Evaluator tool %s failed soft: %s", name, exc)
        return {"tool": name, "ok": False, "error": str(exc)[:300]}
    return {"tool": name, "ok": False, "error": "unhandled"}


def format_tool_results(results: List[Dict[str, Any]]) -> str:
    lines = []
    for row in results:
        name = row.get("tool") or "tool"
        if row.get("denied"):
            lines.append(f"- {name}: DENIED ({row.get('error')})")
            continue
        snippet = str(
            row.get("body") or row.get("analysis") or row.get("error") or row
        )[:400]
        lines.append(f"- {name}: ok={row.get('ok')} {snippet}")
    return "\n".join(lines)


async def collect_situational_context(payload: Dict[str, Any]) -> str:
    intent = payload.get("user_intent") or ""
    names = ["health", "readiness", "web_capability_status"]
    if _needs_external_fact(intent):
        names.append("web_search")
    if _intent_url(intent):
        names.append("jina_reader")
    results = [await execute_evaluator_tool(name, intent=intent) for name in names]
    await _log_tool_use(payload, results)
    return format_tool_results(results)


async def _log_tool_use(payload: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
    process_run_id = payload.get("process_run_id") or ""
    if not process_run_id:
        return
    try:
        from app.db.database import AsyncSessionLocal
        from app.db.models import Observation

        async with AsyncSessionLocal() as db:
            db.add(
                Observation(
                    process_run_id=process_run_id,
                    source="process_evaluator",
                    observation_type="evaluator_tools",
                    payload_ref={
                        "tools": [
                            {
                                "tool": r.get("tool"),
                                "ok": r.get("ok"),
                                "denied": r.get("denied"),
                            }
                            for r in results
                        ]
                    },
                    confidence=100,
                )
            )
            await db.commit()
    except Exception as exc:
        logger.debug("Evaluator tool log skipped: %s", exc)
