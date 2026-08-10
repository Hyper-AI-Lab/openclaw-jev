"""Layer 2 deterministic decisions from vector similarity before LLM."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.config import get_task_registry_config


def vector_similarity_gate(
    context: Dict[str, Any],
    *,
    session_key: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Return synthetic LLM result when vector similarity clearly indicates action.
    None → proceed to LLM (Layer 3).
    """
    cfg = get_task_registry_config()
    min_score = float(cfg.get("similarity_threshold", 0.72))
    hits: List[Dict[str, Any]] = context.get("vector_similar") or []
    active: List[Dict[str, Any]] = context.get("active_tasks") or []
    active_by_id = {t.get("task_id"): t for t in active if t.get("task_id")}

    if not hits:
        return None

    top = hits[0]
    score = float(top.get("score") or 0)
    if score < min_score:
        return None

    task_id = top.get("task_id")
    if not task_id:
        return None

    if task_id in active_by_id:
        target = active_by_id[task_id]
        t_session = target.get("session_key") or ""
        t_kind = target.get("task_kind") or "one_shot"
        same_session = not t_session or not session_key or t_session == session_key
        if same_session or t_kind == "durable":
            return {
                "decision": "attach_active",
                "confidence": min(99, int(score * 100)),
                "rationale": f"Vector match {score:.2f} to active task",
                "target_task_id": task_id,
                "similar_task_ids": [task_id],
            }
        return {
            "decision": "wait_active",
            "confidence": min(99, int(score * 100)),
            "rationale": f"Vector match {score:.2f} to active task (cross-session wait)",
            "target_task_id": task_id,
            "similar_task_ids": [task_id],
        }

    # Completed/historical match — suggest guided create
    registry_by_id = {
        r.get("task_id"): r
        for r in (context.get("recent_registry") or [])
        if r.get("task_id")
    }
    reg = registry_by_id.get(task_id) or {}
    guidance = (reg.get("outcome_summary") or reg.get("goal") or "").strip()
    if not guidance:
        guidance = (
            top.get("intent_snippet") or top.get("summary") or top.get("outcome_summary") or ""
        ).strip()
    recent_ids = [r.get("task_id") for r in registry_by_id.values() if r.get("task_id")]
    similar = [task_id] + [i for i in recent_ids if i != task_id][:2]
    return {
        "decision": "create_guided",
        "confidence": min(90, int(score * 100)),
        "rationale": f"Vector match {score:.2f} to prior completed task",
        "similar_task_ids": similar,
        "guidance_notes": guidance,
    }
