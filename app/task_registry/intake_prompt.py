"""Prompt and schema for task intake sub-agent."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

INTAKE_JSON_SCHEMA = {
    "decision": "clarify|create_fresh|create_guided|attach_active|wait_active|rebuild_stale|skip_valid|skip_noop|supersede|spawn_process",
    "relation_class": "running|finished|memory|new — how this message relates to known work",
    "execution_mode": "conversational|structured_work — how to execute if a new task is created",
    "confidence": "0-100 integer",
    "rationale": "string",
    "similar_task_ids": ["task uuid strings"],
    "catalog_hint": "optional catalog process_type or null",
    "guidance_notes": "optional string for create_guided or the clarify question to ask the user",
    "target_task_id": "optional task id for attach/wait/rebuild/spawn",
    "target_task_ids": ["attach_active only: every running task id this message adds to or changes (usually one)"],
    "web_intent": "optional none|search|fetch|crawl|adaptive_extract|schema_extract|interact — soft hint for web tool routing",
}


def build_intake_prompt(context: Dict[str, Any]) -> str:
    from app.workflows.catalog import CATALOG, soft_catalog_candidates

    intent = context.get("intent") or ""
    soft_hits = soft_catalog_candidates(intent)
    catalog_ids = sorted(CATALOG.keys())
    payload = {
        "incoming_intent": intent,
        "session_key": context.get("session_key"),
        "recurrence_key": context.get("recurrence_key"),
        "tags": context.get("tags"),
        "active_tasks": context.get("active_tasks"),
        "recent_registry": context.get("recent_registry"),
        "vector_similar": context.get("vector_similar"),
        "evidence_pack": context.get("evidence_pack"),
        "memory_hits": context.get("memory_hits"),
        "fts_hits": context.get("fts_hits"),
        "advisory_hits": context.get("advisory_hits"),
        "supplementary_messages": context.get("supplementary_messages"),
        "soft_catalog_candidates": soft_hits,
        "available_catalog_types": catalog_ids,
    }
    return f"""You are the RMP TASK INTAKE ANALYST (not Aura). You decide how this user message relates to work, the way a careful professional operator would.

Hybrid retrieval in evidence_pack / memory_hits / fts_hits / vector_similar is EVIDENCE ONLY.
soft_catalog_candidates and advisory_hits are ADVISORY ONLY — dismiss false positives (awareness, meta chat).

FOUR RELATION CLASSES (set relation_class):
1. running — same currently running/blocked process → attach_active, wait_active, or rebuild_stale
2. finished — related to a completed/failed past task → create_guided (cite task ids)
3. memory — only global/user memory is relevant, no matching task → create_guided or conversational create_fresh with memory citations
4. new — no relevant running/finished/memory match → create_fresh and treat as new work

DECISIONS:
- clarify: you are uncertain which class applies. Put the question in guidance_notes. Do NOT start Aura execution.
- attach_active: continue the running task (target_task_id required). If the message adds to or changes several running tasks, list all of them in target_task_ids.
- wait_active: tell the user work is in flight; do not start a rival workflow.
- rebuild_stale: the active workflow looks stuck/dead; supersede it and restart with this message.
- create_guided: new workflow with citations of finished work and/or memory.
- create_fresh: new Task row for this Slack message in the same conversation (not amnesia). Use conversational mode when it is chat.
- skip_valid / skip_noop: true no-ops only (still logged; a short ack will be sent).
- supersede / spawn_process: as before for recurrent/durable legs.

RULES:
- If confidence < 65 and there is no single obvious active target, use clarify — never default to create_fresh out of uncertainty.
- wait_active only when exactly one clear running target exists.
- execution_mode (when work is created):
  - conversational: greetings, awareness, status with no deliverable — one turn, catalog_hint MUST be null.
  - structured_work: tools, files, research, implement.
- catalog_hint: YOU assign (or null). Valid ids are in available_catalog_types.
  Never set tool_self_upgrade for awareness ("are you aware", "now you can").
- web_intent: search|fetch|crawl|adaptive_extract|schema_extract|interact|none. interact only for real click/login/screenshot.
- Never invent task IDs; use only IDs from context.

Respond with ONLY a single JSON object matching this schema (no markdown fences, no prose before or after):
{json.dumps(INTAKE_JSON_SCHEMA, indent=2)}

CONTEXT:
```json
{json.dumps(payload, default=str)[:16000]}
```

[INTERNAL_RMP]"""


def _extract_json_object(text: str) -> Optional[str]:
    """Brace-balanced extraction of first JSON object containing 'decision'."""
    if not text:
        return None
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    if '"decision"' in candidate or "'decision'" in candidate:
                        return candidate
                    break
        start = text.find("{", start + 1)
    return None


def parse_intake_response(text: str) -> Dict[str, Any]:
    import re

    raw = text or ""
    # fenced json block
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw, re.IGNORECASE)
    if fence:
        raw = fence.group(1)
    blob = _extract_json_object(raw) or raw.strip()
    if blob:
        try:
            parsed = json.loads(blob)
            if isinstance(parsed, dict) and parsed.get("decision"):
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "decision": "clarify",
        "confidence": 0,
        "rationale": "Failed to parse intake JSON",
        "guidance_notes": "I wasn't sure how this relates to current work — could you clarify?",
        "similar_task_ids": [],
    }
