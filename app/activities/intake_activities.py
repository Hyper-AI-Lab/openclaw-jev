"""LLM sub-agent for universal task intake."""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from temporalio import activity

from app.config import get_task_registry_config
from app.task_registry.intake_cache import get_cached, set_cached
from app.task_registry.intake_context import assemble_intake_context
from app.task_registry.intake_decision_engine import apply_intake_policy
from app.task_registry.intake_prompt import build_intake_prompt, parse_intake_response
from app.task_registry.recurrence import (
    derive_recurrence_key,
    fast_path_decision,
    skip_noop_decision,
    skip_valid_decision,
    supersede_decision,
)

logger = logging.getLogger("rmp.task_intake")


def _llm_result_from_gate(
    decision: str, rationale: str, target_id: Optional[str] = None, **extra: Any
) -> Dict[str, Any]:
    return {
        "decision": decision,
        "confidence": 95,
        "rationale": rationale,
        "target_task_id": target_id,
        "similar_task_ids": [target_id] if target_id else [],
        **extra,
    }


async def run_intake_deterministic_gates(
    payload: Dict[str, Any],
    context: Dict[str, Any],
    *,
    recurrence_key: Optional[str],
    fp: str,
) -> Optional[Dict[str, Any]]:
    """Fast-path intake decisions that never call OpenClaw."""
    intent = payload.get("intent") or ""
    session_key = payload.get("session_key") or ""
    tags: List[str] = payload.get("tags") or []
    tag_set = {t.lower() for t in tags}

    fast = fast_path_decision(
        active_tasks=context.get("active_tasks") or [],
        recurrence_key=recurrence_key,
        tags=tags,
        intent=intent,
        session_key=session_key,
    )
    if fast:
        decision, rationale, target_id = fast
        return _llm_result_from_gate(decision, rationale, target_id)

    if "force-canary-run" not in tag_set:
        skip = await skip_valid_decision(recurrence_key)
        if skip:
            decision, rationale, target_id = skip
            return _llm_result_from_gate(decision, rationale, target_id)

    noop = await skip_noop_decision(recurrence_key)
    if noop:
        decision, rationale, target_id = noop
        return _llm_result_from_gate(decision, rationale, target_id)

    sup = await supersede_decision(recurrence_key)
    if sup:
        decision, rationale, target_id = sup
        return _llm_result_from_gate(decision, rationale, target_id)

    from app.task_registry.vector_gate import vector_similarity_gate

    gate = vector_similarity_gate(context, session_key=session_key)
    if gate:
        return gate

    return None


async def _build_intake_context(payload: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[str], str]:
    from app.activities.openclaw_activities import _safe_activity_heartbeat

    intent = payload.get("intent") or ""
    session_key = payload.get("session_key") or ""
    tags: List[str] = payload.get("tags") or []
    recurrence_key = payload.get("recurrence_key") or derive_recurrence_key(
        session_key, intent, tags
    )
    context = await assemble_intake_context(
        intent,
        session_key=session_key,
        recurrence_key=recurrence_key,
        tags=tags,
    )
    _safe_activity_heartbeat()
    context["task_type"] = payload.get("task_type") or ""
    fp = hashlib.sha256(
        f"{session_key}:{recurrence_key}:{intent[:500]}".encode()
    ).hexdigest()
    return context, recurrence_key, fp


async def classify_task_intake_deterministic(payload: Dict[str, Any]) -> Dict[str, Any]:
    """API fallback when IntakeWorkflow is unavailable — no OpenClaw LLM."""
    context, recurrence_key, fp = await _build_intake_context(payload)
    tags: List[str] = payload.get("tags") or []

    cfg = get_task_registry_config()
    cache_sec = int(cfg.get("intake_cache_sec", 60))
    tag_set = {t.lower() for t in tags}
    if "force-canary-run" not in tag_set:
        cached = get_cached(fp, cache_sec)
        if cached:
            return cached

    llm_result = await run_intake_deterministic_gates(
        payload, context, recurrence_key=recurrence_key, fp=fp
    )
    if not llm_result:
        llm_result = {
            "decision": "create_fresh",
            "confidence": 0,
            "rationale": "Intake workflow unavailable; deterministic fallback only",
            "similar_task_ids": [],
        }

    result = apply_intake_policy(llm_result, context, tags=tags)
    result["recurrence_key"] = recurrence_key
    result["request_hash"] = fp
    # Do not cache degraded/zero-confidence fallbacks — they poison the next minute.
    if int(result.get("confidence") or 0) > 0:
        set_cached(fp, result)
    return result


async def classify_task_intake(payload: Dict[str, Any]) -> Dict[str, Any]:
    context, recurrence_key, fp = await _build_intake_context(payload)
    tags: List[str] = payload.get("tags") or []
    process_type_hint = payload.get("process_type_hint")

    cfg = get_task_registry_config()
    cache_sec = int(cfg.get("intake_cache_sec", 60))
    tag_set = {t.lower() for t in tags}
    if "force-canary-run" not in tag_set:
        cached = get_cached(fp, cache_sec)
        if cached:
            return cached

    llm_result = await run_intake_deterministic_gates(
        payload, context, recurrence_key=recurrence_key, fp=fp
    )
    from app.activities.openclaw_activities import _safe_activity_heartbeat

    _safe_activity_heartbeat()

    if not llm_result:
        from app.activities.openclaw_activities import _execute_intake_llm, _safe_activity_heartbeat

        prompt = build_intake_prompt(context)
        if process_type_hint:
            prompt += f"\nPlugin hint process_type: {process_type_hint}\n"
        intake_id = fp[:12]
        _safe_activity_heartbeat()
        try:
            raw = await _execute_intake_llm(intake_id, prompt)
            llm_result = parse_intake_response(raw)
        except Exception as exc:
            logger.warning("Intake LLM failed: %s", exc)
            llm_result = {
                "decision": "create_fresh",
                "confidence": 0,
                "rationale": f"Intake LLM error: {exc}",
                "similar_task_ids": [],
            }
        _safe_activity_heartbeat()

    result = apply_intake_policy(llm_result, context, tags=tags)
    result["recurrence_key"] = recurrence_key
    result["request_hash"] = fp
    if int(result.get("confidence") or 0) > 0:
        set_cached(fp, result)
    return result


@activity.defn(name="classify_task_intake")
async def classify_task_intake_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    return await classify_task_intake(payload)
