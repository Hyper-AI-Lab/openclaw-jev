"""Deterministic enforcement of intake LLM recommendations."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.config import get_task_registry_config, get_task_registry_intake_mode
from app.orchestrator.execution_mode import resolve_execution_mode
from app.workflows.catalog import catalog_type_for_workflow

VALID_DECISIONS = frozenset(
    {
        "create_fresh",
        "create_guided",
        "attach_active",
        "wait_active",
        "skip_valid",
        "skip_noop",
        "supersede",
        "spawn_process",
    }
)


def apply_intake_policy(
    llm_result: Dict[str, Any],
    context: Dict[str, Any],
    *,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    cfg = get_task_registry_config()
    mode = get_task_registry_intake_mode()
    overrides: List[str] = []
    decision = (llm_result.get("decision") or "create_fresh").strip().lower()
    if decision not in VALID_DECISIONS:
        decision = "create_fresh"
        overrides.append("invalid_decision_normalized")

    confidence = int(llm_result.get("confidence") or 0)
    threshold = int(cfg.get("intake_confidence_threshold", 65))
    target_task_id = llm_result.get("target_task_id")
    similar_ids = llm_result.get("similar_task_ids") or []

    active = context.get("active_tasks") or []
    active_ids = {t.get("task_id") for t in active}
    session_key = context.get("session_key") or ""

    tag_set = {t.lower() for t in (tags or [])}
    if "canary" in tag_set and "memory-canary" not in tag_set:
        if decision in ("skip_valid", "skip_noop", "wait_active"):
            decision = "create_fresh"
            overrides.append("canary_never_skip")

    if decision in ("attach_active", "wait_active", "spawn_process") and not target_task_id:
        if active:
            target_task_id = active[0].get("task_id")
        elif similar_ids:
            target_task_id = similar_ids[0]
        else:
            decision = "create_fresh"
            overrides.append("attach_without_target")

    if target_task_id and target_task_id not in active_ids and decision in (
        "attach_active",
        "wait_active",
    ):
        decision = "create_fresh"
        overrides.append("target_not_active")

    if decision in ("attach_active", "wait_active") and target_task_id:
        for t in active:
            if t.get("task_id") == target_task_id:
                t_session = t.get("session_key") or ""
                t_kind = t.get("task_kind") or "one_shot"
                if t_session and session_key and t_session != session_key:
                    if t_kind != "durable":
                        decision = "create_fresh"
                        overrides.append("cross_session_attach_denied")
                break

    if confidence < threshold and decision not in ("create_fresh", "wait_active"):
        if active:
            decision = "wait_active"
            target_task_id = active[0].get("task_id")
            overrides.append("low_confidence_wait")
        else:
            decision = "create_fresh"
            overrides.append("low_confidence_create")

    catalog_hint = llm_result.get("catalog_hint")
    catalog_type = None
    if catalog_hint:
        catalog_type = catalog_type_for_workflow(
            str(catalog_hint),
            context.get("intent") or "",
            str(catalog_hint),
        )
        if not catalog_type:
            overrides.append("invalid_catalog_hint_ignored")

    enforced = mode == "enforce"
    effective = decision if enforced else "create_fresh"
    if mode == "shadow" and decision != "create_fresh":
        overrides.append(f"shadow_would_{decision}")

    execution_mode = resolve_execution_mode(
        intent=context.get("intent") or "",
        tags=tags,
        task_type=str(context.get("task_type") or ""),
        llm_mode=llm_result.get("execution_mode"),
        catalog_type=catalog_type,
        llm_result=llm_result,
    )

    return {
        "decision": decision,
        "effective_decision": effective if enforced else "create_fresh",
        "confidence": confidence,
        "rationale": llm_result.get("rationale") or "",
        "similar_task_ids": similar_ids,
        "target_task_id": target_task_id,
        "catalog_type": catalog_type,
        "guidance_notes": llm_result.get("guidance_notes") or "",
        "policy_overrides": overrides,
        "intake_mode": mode,
        "llm_raw": llm_result,
        "execution_mode": execution_mode,
    }
