"""Process plan generation — single LLM call, program-owned plan thereafter."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from app.activities.openclaw_activities import send_to_openclaw
from app.orchestrator.execution_mode import (
    EXECUTION_CONVERSATIONAL,
    EXECUTION_STRUCTURED,
    normalize_execution_mode,
)
from app.telemetry import traced_activity
from temporalio import activity


DEFAULT_PLAN_STEPS = [
    {
        "name": "gather",
        "kind": "gather_facts",
        "predicate_id": "gather_facts",
        "prompt": "Gather facts needed to answer the user. Do not deliver the final answer yet.",
    },
    {
        "name": "execute",
        "kind": "deliver",
        "predicate_id": "deliver",
        "prompt": "Using gathered context, produce the final answer for the user.",
    },
]

CONVERSATIONAL_DELIVER_STEP = {
    "name": "deliver",
    "kind": "deliver",
    "predicate_id": "generic_deliver",
    "prompt": (
        "Reply naturally to the user in clear English. "
        "Be concise — one short paragraph for greetings, status updates, or simple chat. "
        "Do not run broad filesystem scans or codebase exploration unless the user explicitly "
        "asked for an audit with concrete deliverables."
    ),
}

HEALTH_CANARY_DELIVER_STEP = {
    "name": "deliver",
    "kind": "deliver",
    "predicate_id": "deliver",
    "prompt": (
        "Reply with exactly CANARY_OK on its own line. No tools, no extra text."
    ),
}


def _extract_plan_json_object(text: str) -> Optional[str]:
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
                    if '"steps"' in candidate:
                        return candidate
                    break
        start = text.find("{", start + 1)
    return None


def _parse_plan_json(text: str) -> List[Dict[str, Any]]:
    raw = text or ""
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
    if fence:
        raw = fence.group(1)
    blob = _extract_plan_json_object(raw) or raw.strip()
    if blob:
        try:
            parsed = json.loads(blob)
            steps = parsed.get("steps") or []
            if isinstance(steps, list) and steps:
                return steps
        except json.JSONDecodeError:
            pass
    return []


@traced_activity("plan.generate")
async def generate_process_plan(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build execution steps from intake execution_mode; plan LLM only for structured work."""
    task_id = payload.get("task_id", "unknown")
    user_intent = payload.get("intent", "")
    session_key = payload.get("session_key", "agent:main:main")
    task_type = (payload.get("task_type") or "").lower()
    tags = {str(t).lower() for t in (payload.get("tags") or [])}
    execution_mode = normalize_execution_mode(payload.get("execution_mode")) or EXECUTION_STRUCTURED

    if "memory canary" in (user_intent or "").lower() or "memory-canary" in tags:
        return {
            "steps": [
                {
                    "name": "deliver",
                    "kind": "deliver",
                    "predicate_id": "deliver",
                    "prompt": (
                        "Use ONLY the PROCESS-SCOPED MEMORY block in the prompt. "
                        "Do not use memory_search or read workspace files. "
                        "Reply with one English sentence summarizing what process memory contains."
                    ),
                },
            ],
            "source": "deterministic_memory_canary",
            "version": 1,
        }

    if (task_type == "canary" or tags & {"canary", "system"}) and "memory-canary" not in tags:
        return {
            "steps": [HEALTH_CANARY_DELIVER_STEP],
            "source": "deterministic_health_canary",
            "version": 1,
        }

    if execution_mode == EXECUTION_CONVERSATIONAL:
        return {
            "steps": [CONVERSATIONAL_DELIVER_STEP],
            "source": "intake_conversational",
            "version": 1,
        }

    plan_prompt = f"""Create a minimal execution plan for this user request (2-4 steps max).
User request: {user_intent}

Reply with ONLY JSON (no tool calls):
{{"steps": [{{"name": "...", "kind": "gather_facts|file_read|summarize|deliver", "predicate_id": "...", "prompt": "..."}}]}}

Use predicate_id one of: gather_facts, file_read, summarize, deliver, catalog_dispatch.
Do NOT use memory_search or workspace files when planning.
[INTERNAL_RMP]"""

    try:
        response = await send_to_openclaw(
            {"message": plan_prompt, "task_id": task_id, "session_key": session_key}
        )
        text = ""
        if "result" in response and "payloads" in response["result"]:
            text = response["result"]["payloads"][0].get("text", "")
        steps = _parse_plan_json(text)
        if steps:
            return {"steps": steps, "source": "plan_llm", "version": 1}
    except Exception as exc:
        activity.logger.warning(
            "plan LLM failed for task=%s; using deterministic fallback: %s",
            task_id,
            exc,
        )

    return {"steps": list(DEFAULT_PLAN_STEPS), "source": "deterministic_fallback", "version": 1}


@traced_activity("plan.save")
async def save_process_plan(payload: Dict[str, Any]) -> bool:
    from sqlalchemy import select

    from app.db.database import AsyncSessionLocal
    from app.db.models import ProcessRun

    process_run_id = payload.get("process_run_id")
    plan = payload.get("plan") or {}
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ProcessRun).where(ProcessRun.id == process_run_id)
        )
        run = result.scalar_one_or_none()
        if not run:
            return False
        run.plan_json = plan
        await db.commit()
    return True
