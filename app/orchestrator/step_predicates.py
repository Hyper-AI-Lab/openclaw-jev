"""Deterministic step completion predicates — program decides, not LLM task_status."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


def extract_agent_facts(text: str) -> Dict[str, Any]:
    """Parse structured facts JSON from agent output; fall back to legacy task_status."""
    blob = text or ""
    facts: Dict[str, Any] = {}
    for candidate in _json_candidates(blob):
        try:
            parsed = candidate
            if "facts" in parsed:
                facts = parsed.get("facts") or {}
                break
        except (json.JSONDecodeError, TypeError):
            pass

    legacy_status = "pending"
    legacy_reason = ""
    for candidate in _json_candidates(blob):
        try:
            parsed = candidate
            if "task_status" in parsed:
                legacy_status = parsed.get("task_status", "pending")
                legacy_reason = parsed.get("reason", "")
                break
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "facts": facts,
        "legacy_status": legacy_status,
        "legacy_reason": legacy_reason,
        "body": _strip_json_blocks(blob),
    }


def _json_candidates(blob: str) -> List[Dict[str, Any]]:
    """Yield parsed JSON objects from fenced or inline blocks."""
    out: List[Dict[str, Any]] = []
    for match in re.finditer(r"```json\s*(\{[\s\S]*?\})\s*```", blob, re.IGNORECASE):
        try:
            out.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            pass
    for match in re.finditer(r"(\{[^{}]*\"(?:facts|task_status)\"[\s\S]*?\})", blob):
        try:
            out.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            pass
    # Greedy last-object fallback for nested facts
    start = blob.rfind("{")
    while start >= 0:
        fragment = blob[start:]
        try:
            out.append(json.loads(fragment))
            break
        except json.JSONDecodeError:
            start = blob.rfind("{", 0, start)
    return out


def _strip_json_blocks(text: str) -> str:
    cleaned = re.sub(
        r"```json\s*\{[\s\S]*?\}\s*```", "", text, flags=re.DOTALL | re.IGNORECASE
    )
    for marker in ('"facts"', '"task_status"'):
        while True:
            start = cleaned.rfind("{")
            removed = False
            while start >= 0:
                fragment = cleaned[start:]
                if marker not in fragment:
                    start = cleaned.rfind("{", 0, start)
                    continue
                try:
                    json.loads(fragment)
                    cleaned = cleaned[:start].strip()
                    removed = True
                    break
                except json.JSONDecodeError:
                    start = cleaned.rfind("{", 0, start)
            if not removed:
                break
    return cleaned.strip()


def evaluate_step_predicate(
    predicate_id: str,
    *,
    user_intent: str,
    agent_text: str,
    facts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return passed, suggested_status, issues."""
    facts = facts or {}
    body = _strip_json_blocks(agent_text)
    issues: List[str] = []
    pid = (predicate_id or "generic_deliver").strip()

    upper_body = (body or "").strip().upper()
    if "CANARY_OK" in upper_body or "RMP CANARY" in (user_intent or "").upper():
        return {"passed": True, "suggested_status": "completed", "issues": []}

    if len(body.strip()) < 20:
        issues.append("Response too short for step completion")

    if pid == "file_read":
        path = facts.get("file_path") or facts.get("path") or ""
        if not path and not re.search(r"/[\w./-]+\.(md|txt|json|py|js)", body, re.I):
            issues.append("file_read: no file path in facts or response")
        if facts.get("read_ok") is False:
            issues.append("file_read: read_ok=false")

    elif pid == "summarize":
        if len(body) < 80:
            issues.append("summarize: summary too short")

    elif pid == "gather_facts":
        if not facts and len(body) < 40:
            issues.append("gather_facts: insufficient factual content")

    elif pid == "catalog_dispatch":
        if facts.get("blocked") is True:
            return {"passed": True, "suggested_status": "blocked", "issues": []}
        if facts.get("step_complete") is False:
            issues.append("catalog_dispatch: step_complete=false in facts")

    elif pid == "deliver" or pid == "generic_deliver":
        if len(body) < 30:
            issues.append("deliver: no substantive answer")

    passed = len(issues) == 0
    suggested = "completed" if passed else "pending"
    if facts.get("blocked"):
        suggested = "blocked"
    elif facts.get("failed"):
        suggested = "failed"

    return {"passed": passed, "suggested_status": suggested, "issues": issues}


def decide_status_from_predicates(
    predicate_id: str,
    user_intent: str,
    agent_text: str,
    validation_ok: bool,
    attempt: int,
    max_attempts: int,
) -> Dict[str, Any]:
    """Combine facts extraction + predicate evaluation + validation."""
    extracted = extract_agent_facts(agent_text)
    if not validation_ok:
        if attempt >= max_attempts:
            return {
                "status": "failed",
                "reason": "Output validation failed",
                "action": "fail",
            }
        return {"status": "pending", "reason": "Output validation failed", "action": "retry"}

    pred = evaluate_step_predicate(
        predicate_id,
        user_intent=user_intent,
        agent_text=agent_text,
        facts=extracted.get("facts"),
    )
    if pred["passed"]:
        status = pred["suggested_status"]
        if status == "completed":
            return {"status": "completed", "reason": "Predicate passed", "action": "complete"}
        if status == "blocked":
            return {
                "status": "blocked",
                "reason": extracted.get("legacy_reason") or "Blocked",
                "action": "blocked",
            }

    if attempt >= max_attempts:
        return {
            "status": "failed",
            "reason": "; ".join(pred["issues"]) or "Max attempts",
            "action": "fail",
        }
    return {
        "status": "pending",
        "reason": "; ".join(pred["issues"]) or extracted.get("legacy_reason", "Retry"),
        "action": "retry",
    }
