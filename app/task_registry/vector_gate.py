"""Layer 2 vector similarity is advisory evidence only — never assigns a workflow."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.config import get_task_registry_config


def advisory_vector_hits(
    context: Dict[str, Any],
    *,
    session_key: str = "",
) -> List[Dict[str, Any]]:
    """Ranked vector hits for the Intake Analyst prompt. Not a decision."""
    cfg = get_task_registry_config()
    min_score = float(cfg.get("similarity_threshold", 0.72))
    hits: List[Dict[str, Any]] = list(context.get("vector_similar") or [])
    out: List[Dict[str, Any]] = []
    for hit in hits[:8]:
        row = dict(hit)
        row["advisory"] = True
        row["above_threshold"] = float(hit.get("score") or 0) >= min_score
        out.append(row)
    return out


def vector_similarity_gate(
    context: Dict[str, Any],
    *,
    session_key: str = "",
) -> Optional[Dict[str, Any]]:
    """Never auto-attach/create. Stash advisory hits on context and proceed to LLM."""
    context["advisory_hits"] = advisory_vector_hits(context, session_key=session_key)
    return None
