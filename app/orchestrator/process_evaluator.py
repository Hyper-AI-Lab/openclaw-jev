"""Process Evaluator — non-Aura judge. Fail closed on parse errors."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger("rmp.process_evaluator")

VALID_VERDICTS = frozenset({"accept", "rework", "strategy_change", "escalate_user"})

EVALUATOR_JSON_SCHEMA = {
    "verdict": "accept|rework|strategy_change|escalate_user",
    "quality": "pass|fail",
    "reason": "string",
    "issues": "string if not accept",
    "command_to_aura": "concrete instruction for Aura when not accept",
}


def evaluator_error_result(reason: str = "evaluator error") -> Dict[str, str]:
    return {
        "verdict": "rework",
        "quality": "fail",
        "issues": reason,
        "reason": reason,
        "command_to_aura": "Retry the user ask. Prior evaluator output was unusable.",
        "parse_error": True,
    }


def _extract_json_object(text: str) -> Optional[str]:
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
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def parse_evaluator_response(text: str) -> Dict[str, str]:
    """Fail closed: unparsable judge output is rework, never accept."""
    raw = text or ""
    if raw.lower().startswith("error:"):
        return evaluator_error_result(f"evaluator error: {raw[:300]}")
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw, re.IGNORECASE)
    blob = (fence.group(1) if fence else None) or _extract_json_object(raw)
    parsed: Dict[str, Any] = {}
    if blob:
        try:
            loaded = json.loads(blob)
            if isinstance(loaded, dict):
                parsed = loaded
        except json.JSONDecodeError:
            parsed = {}
    if not parsed:
        return evaluator_error_result("evaluator error: malformed judge JSON")

    verdict = str(parsed.get("verdict") or "").strip().lower()
    quality = str(parsed.get("quality") or "").strip().lower()
    if verdict not in VALID_VERDICTS:
        if quality == "pass":
            verdict = "accept"
        elif quality == "fail":
            verdict = "rework"
        else:
            return evaluator_error_result("evaluator error: missing verdict")
    if verdict == "accept":
        quality = "pass"
    else:
        quality = "fail"
    return {
        "verdict": verdict,
        "quality": quality,
        "reason": str(parsed.get("reason") or "")[:1000],
        "issues": str(parsed.get("issues") or parsed.get("reason") or "")[:1000],
        "command_to_aura": str(parsed.get("command_to_aura") or "")[:2000],
        "parse_error": False,
    }


def build_evaluator_prompt(payload: Dict[str, Any]) -> str:
    attempt = payload.get("attempt", 1)
    brief = (payload.get("process_brief") or payload.get("initial_memory_block") or "").strip()
    tools = payload.get("tools_taken") or payload.get("actions_taken") or ""
    artifacts = payload.get("artifacts") or ""
    situational = (payload.get("situational_tools") or "").strip()
    return f"""You are the RMP PROCESS EVALUATOR (not Aura). Aura's reply must NOT reach Slack until you accept it.

USER ASK:
{payload.get("user_intent") or ""}

PROCESS BRIEF:
{brief or "(none)"}

AURA OUTPUT:
{payload.get("agent_response") or ""}

TOOLS/ACTIONS TAKEN:
{tools or "(not provided)"}

ARTIFACTS:
{artifacts or "(not provided)"}

SITUATIONAL TOOLS (local health/readiness/web only; privileged ops are denied):
{situational or "(not fetched)"}

ATTEMPT: {attempt}

Greetings/social chat: accept a short matching reply. Do not skip this judgment.
Insufficient work: verdict=rework with a concrete command_to_aura.
Around attempt 10 you may verdict=strategy_change. Around attempt 20, verdict=escalate_user.

Respond with ONLY a JSON object:
{json.dumps(EVALUATOR_JSON_SCHEMA, indent=2)}

[INTERNAL_RMP]"""


async def persist_evaluator_verdict(payload: Dict[str, Any], result: Dict[str, str]) -> None:
    try:
        from app.db.database import AsyncSessionLocal
        from app.db.models import Event, Observation

        task_id = payload.get("task_id") or "unknown"
        process_run_id = payload.get("process_run_id") or ""
        event_payload = {
            "verdict": result.get("verdict"),
            "quality": result.get("quality"),
            "issues": (result.get("issues") or "")[:500],
            "attempt": payload.get("attempt"),
            "parse_error": result.get("parse_error"),
            "command_to_aura": (result.get("command_to_aura") or "")[:500],
        }
        verdict = (result.get("verdict") or "").strip().lower()
        extra_type = ""
        if verdict == "accept":
            extra_type = "evaluator.accept"
        elif verdict == "escalate_user":
            extra_type = "evaluator.escalate"
        async with AsyncSessionLocal() as db:
            db.add(
                Event(
                    correlation_id=task_id,
                    entity_type="task",
                    entity_id=task_id,
                    event_type="evaluator.verdict",
                    event_payload=event_payload,
                )
            )
            if extra_type:
                db.add(
                    Event(
                        correlation_id=task_id,
                        entity_type="task",
                        entity_id=task_id,
                        event_type=extra_type,
                        event_payload=event_payload,
                    )
                )
            if process_run_id:
                db.add(
                    Observation(
                        process_run_id=process_run_id,
                        source="process_evaluator",
                        observation_type="evaluator_verdict",
                        payload_ref=event_payload,
                        confidence=100,
                    )
                )
            if task_id and task_id != "unknown":
                from app.db.models import TaskMessage
                import uuid as _uuid

                cmd = result.get("command_to_aura") or result.get("reason") or verdict
                db.add(
                    TaskMessage(
                        id=str(_uuid.uuid4()),
                        task_id=task_id,
                        role="evaluator",
                        content=(cmd or "verdict")[:4000],
                        source="process_evaluator",
                    )
                )
            await db.commit()
    except Exception as exc:
        logger.warning("Persist evaluator verdict failed: %s", exc)
