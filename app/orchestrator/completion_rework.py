"""Structured rejection payloads for completion rework loops."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def get_rework_max_attempts() -> int:
    from app.config import get_task_registry_config

    return int(get_task_registry_config().get("rework_max_attempts", 3))


def build_rework_prompt(
    user_intent: str,
    prior_response: str,
    *,
    evidence_issues: Optional[List[str]] = None,
    quality_issues: str = "",
    attempt: int = 1,
    max_attempts: int = 3,
) -> str:
    issues = evidence_issues or []
    block = [
        "COMPLETION REJECTED — revise your answer or admit you cannot complete.",
        f"Attempt {attempt} of {max_attempts}.",
        f"ORIGINAL REQUEST:\n{user_intent[:1500]}",
    ]
    if issues:
        block.append("EVIDENCE ISSUES:\n- " + "\n- ".join(issues))
    if quality_issues:
        block.append(f"QUALITY ISSUES:\n{quality_issues}")
    block.append(f"YOUR PRIOR RESPONSE:\n{(prior_response or '')[:2000]}")
    block.append(
        "Respond with a corrected user-facing answer. "
        "If impossible, state clearly that you cannot complete and why. "
        "End with facts JSON: ```json\n{\"facts\": {\"step_complete\": true}}\n```"
    )
    return "\n\n".join(block)


def should_admit_failure(attempt: int, max_attempts: int, response: str) -> bool:
    if attempt >= max_attempts:
        return True
    lower = (response or "").lower()
    return any(
        p in lower
        for p in (
            "cannot complete",
            "can't complete",
            "unable to complete",
            "cannot do this",
            "not possible",
        )
    )
