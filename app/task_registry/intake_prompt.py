"""Prompt and schema for task intake sub-agent."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

INTAKE_JSON_SCHEMA = {
    "decision": "create_fresh|create_guided|attach_active|wait_active|skip_valid|skip_noop|supersede|spawn_process",
    "execution_mode": "conversational|structured_work — how to execute if a new task is created",
    "confidence": "0-100 integer",
    "rationale": "string",
    "similar_task_ids": ["task uuid strings"],
    "catalog_hint": "optional catalog process_type or null",
    "guidance_notes": "optional string for create_guided",
    "target_task_id": "optional task id for attach/wait/spawn",
}


def build_intake_prompt(context: Dict[str, Any]) -> str:
    payload = {
        "incoming_intent": context.get("intent"),
        "session_key": context.get("session_key"),
        "recurrence_key": context.get("recurrence_key"),
        "tags": context.get("tags"),
        "active_tasks": context.get("active_tasks"),
        "recent_registry": context.get("recent_registry"),
        "vector_similar": context.get("vector_similar"),
        "supplementary_messages": context.get("supplementary_messages"),
    }
    return f"""You are the RMP TASK INTAKE adjudicator. Decide whether to create a new task, attach to an active one, wait, skip, or supersede.

RULES:
- Prefer attach_active or wait_active when the same work is already running.
- Prefer skip_valid when a recent completed recurrent job still satisfies the request (e.g. nothing new to check).
- Prefer create_guided when similar history exists but the situation may have changed.
- Prefer supersede when a failed/stale recurrent instance should be replaced.
- Use skip_noop only when truly nothing actionable is needed.
- execution_mode (required when decision creates work):
  - conversational: social chat, greetings, acknowledgements, dev updates with no concrete deliverable — reply in one turn, no filesystem/code exploration.
  - structured_work: needs tools, files, research, multi-step execution, or explicit "explore/read/implement" asks.
- catalog_hint is optional (browser_automation, outreach, etc.) — only if clearly applicable.
- Never invent task IDs; use only IDs from context.

Respond with ONLY a single JSON object matching this schema (no markdown fences, no prose before or after):
{json.dumps(INTAKE_JSON_SCHEMA, indent=2)}

CONTEXT:
```json
{json.dumps(payload, default=str)[:12000]}
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
        "decision": "create_fresh",
        "confidence": 0,
        "rationale": "Failed to parse intake JSON",
        "similar_task_ids": [],
    }
