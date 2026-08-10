"""Deterministic task/step status transitions from evidence and parsed agent output."""
from __future__ import annotations

from typing import Any, Dict, Optional

TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "stopped_by_user", "compensated", "cancelled"}
)
RETRYABLE_STATUSES = frozenset({"pending", "needs_replan"})


def normalize_agent_status(raw_status: str) -> str:
    status = (raw_status or "pending").strip().lower()
    allowed = {
        "completed",
        "pending",
        "failed",
        "stopped_by_user",
        "blocked",
        "needs_replan",
    }
    return status if status in allowed else "pending"


def decide_step_outcome(
    *,
    parsed_status: str,
    reason: str,
    validation_ok: bool,
    attempt: int,
    max_attempts: int,
) -> Dict[str, Any]:
    """Orchestrator decision for a single agent dispatch (generic or catalog step)."""
    status = normalize_agent_status(parsed_status)

    if not validation_ok:
        if attempt >= max_attempts:
            return {
                "action": "fail",
                "status": "failed",
                "reason": reason or "Output validation failed after max attempts",
            }
        return {
            "action": "retry",
            "status": "pending",
            "reason": reason or "Output validation failed",
        }

    if status == "stopped_by_user":
        return {"action": "stop", "status": status, "reason": reason}

    if status == "blocked":
        return {"action": "blocked", "status": status, "reason": reason}

    if status == "failed":
        return {"action": "fail", "status": status, "reason": reason}

    if status == "completed":
        return {"action": "complete", "status": status, "reason": reason}

    if status in RETRYABLE_STATUSES:
        if attempt >= max_attempts:
            return {
                "action": "fail",
                "status": "failed",
                "reason": reason or f"Max attempts ({max_attempts}) reached",
            }
        return {"action": "retry", "status": status, "reason": reason}

    if attempt >= max_attempts:
        return {
            "action": "fail",
            "status": "failed",
            "reason": reason or "Unknown status after max attempts",
        }
    return {"action": "retry", "status": "pending", "reason": reason or "Retry needed"}


def decide_completion_gate(
    *,
    evidence_passed: bool,
    evidence_issues: list,
    quality_passed: Optional[bool],
    quality_issues: str = "",
    skip_quality_llm: bool = False,
) -> Dict[str, Any]:
    """Code-first completion gate: evidence must pass; quality LLM optional."""
    if not evidence_passed:
        return {
            "action": "retry",
            "reason": f"Evidence check failed: {'; '.join(evidence_issues)}",
        }
    if skip_quality_llm or quality_passed is None:
        return {"action": "complete", "reason": "Evidence passed (quality LLM skipped)"}
    if quality_passed is False:
        return {
            "action": "retry",
            "reason": f"Quality check failed: {quality_issues or 'issues detected'}",
        }
    return {"action": "complete", "reason": "Evidence and quality passed"}


def merge_evaluation_with_decision(
    evaluation: Dict[str, str], decision: Dict[str, Any]
) -> Dict[str, str]:
    """Apply orchestrator decision over raw LLM parse result."""
    out = dict(evaluation)
    if decision.get("status"):
        out["status"] = decision["status"]
    if decision.get("reason"):
        out["reason"] = decision["reason"]
    out["orchestrator_action"] = decision.get("action", "")
    return out
