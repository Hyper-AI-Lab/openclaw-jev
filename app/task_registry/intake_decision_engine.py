"""Deterministic enforcement of intake LLM recommendations."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.config import get_task_registry_config, get_task_registry_intake_mode
from app.notification_policy import INTERNAL_TAGS, is_internal_task
from app.orchestrator.execution_mode import intake_llm_failed, resolve_execution_mode
from app.task_registry.session_identity import session_keys_equivalent

VALID_DECISIONS = frozenset(
    {
        "clarify",
        "create_fresh",
        "create_guided",
        "attach_active",
        "wait_active",
        "rebuild_stale",
        "skip_valid",
        "skip_noop",
        "supersede",
        "spawn_process",
    }
)

_RUNNING_DECISIONS = frozenset(
    {"attach_active", "wait_active", "rebuild_stale", "spawn_process"}
)
_CANARY_FORCE_FRESH = frozenset(
    {
        "skip_valid",
        "skip_noop",
        "wait_active",
        "clarify",
        "rebuild_stale",
    }
)


def _incoming_is_internal(tags: Optional[List[str]]) -> bool:
    return bool({str(t).lower() for t in (tags or [])} & INTERNAL_TAGS)


def _active_is_internal(task: Dict[str, Any]) -> bool:
    return is_internal_task(
        str(task.get("goal") or task.get("goal_snippet") or ""),
        str(task.get("task_type") or ""),
        [],
    )


def user_visible_active_tasks(
    active: List[Dict[str, Any]],
    *,
    tags: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Canary/heartbeat/system jobs are not user work — never wait/attach to them."""
    if _incoming_is_internal(tags):
        return list(active)
    return [t for t in active if not _active_is_internal(t)]


def _same_session_actives(
    active: List[Dict[str, Any]], session_key: str
) -> List[Dict[str, Any]]:
    if not session_key:
        return list(active)
    out = []
    for t in active:
        t_session = t.get("session_key") or ""
        t_kind = t.get("task_kind") or "one_shot"
        if session_keys_equivalent(session_key, t_session) or t_kind == "durable":
            out.append(t)
    return out


def _single_clear_active_target(
    active: List[Dict[str, Any]], session_key: str
) -> Optional[Dict[str, Any]]:
    """One running/blocked target on this session — not a pending clarify."""
    candidates = []
    for t in _same_session_actives(active, session_key):
        st = (t.get("status") or "running").lower()
        if st in {"pending_user_input"}:
            continue
        if st in {"created", "running", "pending", "blocked", "needs_replan"} or not st:
            candidates.append(t)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _pending_clarify_target(
    active: List[Dict[str, Any]], session_key: str
) -> Optional[Dict[str, Any]]:
    pending = [
        t
        for t in _same_session_actives(active, session_key)
        if (t.get("status") or "").lower() == "pending_user_input"
    ]
    flagged = [t for t in pending if t.get("intake_clarify")]
    if len(flagged) == 1:
        return flagged[0]
    if len(pending) == 1:
        return pending[0]
    return None


def _relation_class_for(decision: str, llm_result: Dict[str, Any]) -> str:
    raw = (llm_result.get("relation_class") or "").strip().lower()
    if raw in {"running", "finished", "memory", "new"}:
        return raw
    if decision in _RUNNING_DECISIONS:
        return "running"
    if decision == "create_guided":
        return "finished"
    if decision == "create_fresh":
        return "new"
    return "new"


def apply_intake_policy(
    llm_result: Dict[str, Any],
    context: Dict[str, Any],
    *,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    cfg = get_task_registry_config()
    mode = get_task_registry_intake_mode()
    overrides: List[str] = []
    decision = (llm_result.get("decision") or "").strip().lower()
    if decision not in VALID_DECISIONS:
        decision = "clarify"
        overrides.append("invalid_decision_normalized")

    confidence = int(llm_result.get("confidence") or 0)
    threshold = int(cfg.get("intake_confidence_threshold", 65))
    target_task_id = llm_result.get("target_task_id")
    similar_ids = llm_result.get("similar_task_ids") or []

    raw_active = context.get("active_tasks") or []
    active = user_visible_active_tasks(raw_active, tags=tags)
    active_ids = {t.get("task_id") for t in active}
    session_key = context.get("session_key") or ""

    tag_set = {t.lower() for t in (tags or [])}
    is_canary = "canary" in tag_set and "memory-canary" not in tag_set
    degraded = intake_llm_failed(llm_result)
    if is_canary and decision in _CANARY_FORCE_FRESH:
        decision = "create_fresh"
        overrides.append("canary_never_skip")

    clarify_follow = None if is_canary else _pending_clarify_target(active, session_key)
    if clarify_follow:
        decision = "attach_active"
        target_task_id = clarify_follow.get("task_id")
        overrides.append("clarify_followup_attach")

    needs_target = decision in (
        "attach_active",
        "wait_active",
        "spawn_process",
        "rebuild_stale",
    )
    if needs_target and not target_task_id:
        if active:
            target_task_id = active[0].get("task_id")
        elif similar_ids:
            target_task_id = similar_ids[0]
        elif raw_active and not active and not is_canary:
            decision = "create_fresh"
            overrides.append("internal_active_ignored")
        else:
            decision = "clarify" if not is_canary else "create_fresh"
            overrides.append("attach_without_target")

    if (
        target_task_id
        and target_task_id not in active_ids
        and decision in ("attach_active", "wait_active", "rebuild_stale")
    ):
        raw_hit = next(
            (t for t in raw_active if t.get("task_id") == target_task_id),
            None,
        )
        if raw_hit and _active_is_internal(raw_hit) and not is_canary:
            decision = "create_fresh"
            target_task_id = None
            overrides.append("internal_active_ignored")
        else:
            decision = "clarify" if not is_canary else "create_fresh"
            overrides.append("target_not_active")

    if decision in ("attach_active", "wait_active", "rebuild_stale") and target_task_id:
        for t in active:
            if t.get("task_id") == target_task_id:
                t_session = t.get("session_key") or ""
                t_kind = t.get("task_kind") or "one_shot"
                if (
                    t_session
                    and session_key
                    and not session_keys_equivalent(session_key, t_session)
                ):
                    if t_kind != "durable":
                        decision = "clarify" if not is_canary else "create_fresh"
                        overrides.append("cross_session_attach_denied")
                break

    if (
        degraded
        and not is_canary
        and "clarify_followup_attach" not in overrides
    ):
        # Still a full RMP task (POST /tasks → Temporal → rmp_task_* → RMP Slack).
        # Do not wait_active, and do not answer via native OpenClaw Slack.
        decision = "create_fresh"
        target_task_id = None
        overrides.append("degraded_intake_create_fresh")

    if (
        not is_canary
        and not degraded
        and confidence < threshold
        and decision not in (
            "clarify",
            "skip_valid",
            "skip_noop",
        )
    ):
        single = _single_clear_active_target(active, session_key)
        if single and decision in ("attach_active", "wait_active", "rebuild_stale"):
            decision = "wait_active"
            target_task_id = single.get("task_id")
            overrides.append("low_confidence_wait")
        elif single and len(_same_session_actives(active, session_key)) == 1:
            decision = "wait_active"
            target_task_id = single.get("task_id")
            overrides.append("low_confidence_wait")
        else:
            decision = "clarify"
            overrides.append("low_confidence_clarify")

    # No one can answer a clarify question on a scheduled run.
    non_interactive = (
        str(context.get("task_type") or "") == "cron"
        or "cron" in tag_set
        or session_key.startswith("agent:main:cron:")
    )
    if non_interactive and decision == "clarify":
        decision = "create_fresh"
        target_task_id = None
        overrides.append("non_interactive_no_clarify")

    catalog_hint = llm_result.get("catalog_hint")
    catalog_type = None
    raw_hint = (str(catalog_hint).strip() if catalog_hint is not None else "") or ""
    if raw_hint.lower() in ("null", "none", "nil", ""):
        raw_hint = ""
    if raw_hint and decision not in ("clarify",):
        # Intake LLM is the adjudicator: accept a known catalog id/alias.
        # Do NOT require keyword intent match (and do not pass hint as task_type,
        # which would force-match via catalog_type_for_workflow).
        from app.workflows.catalog import get_template, normalize_catalog_type

        catalog_type = normalize_catalog_type(raw_hint, "")
        if not catalog_type or not get_template(catalog_type):
            catalog_type = None
            overrides.append("invalid_catalog_hint_ignored")
        else:
            overrides.append("catalog_from_intake_llm")

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

    relation_class = _relation_class_for(decision, llm_result)

    target_task_ids: List[str] = [target_task_id] if target_task_id else []
    if decision == "attach_active" and target_task_id:
        by_id = {t.get("task_id"): t for t in active}
        for tid in llm_result.get("target_task_ids") or []:
            extra = by_id.get(tid)
            if extra is None or tid in target_task_ids:
                continue
            extra_session = extra.get("session_key") or ""
            if (
                extra_session
                and session_key
                and not session_keys_equivalent(session_key, extra_session)
                and (extra.get("task_kind") or "one_shot") != "durable"
            ):
                overrides.append("cross_session_extra_target_dropped")
                continue
            target_task_ids.append(tid)

    result = {
        "decision": decision,
        "effective_decision": effective if enforced else "create_fresh",
        "confidence": confidence,
        "rationale": llm_result.get("rationale") or "",
        "similar_task_ids": similar_ids,
        "target_task_id": target_task_id,
        "target_task_ids": target_task_ids,
        "catalog_type": catalog_type,
        "guidance_notes": llm_result.get("guidance_notes") or "",
        "policy_overrides": overrides,
        "intake_mode": mode,
        "llm_raw": llm_result,
        "execution_mode": execution_mode,
        "relation_class": relation_class,
    }
    from app.orchestrator.web_capability import merge_web_into_intake

    return merge_web_into_intake(
        result,
        context.get("intent") or "",
        llm_result=llm_result,
    )
