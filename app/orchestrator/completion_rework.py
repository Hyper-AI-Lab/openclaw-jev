"""Structured rejection payloads for completion rework loops."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def get_attempt_policy() -> Dict[str, int]:
    from app.config import get_task_registry_config

    cfg = get_task_registry_config()
    max_attempts = int(cfg.get("rework_max_attempts", 20))
    return {
        "max_attempts": max_attempts,
        "strategy_change_attempt": int(cfg.get("strategy_change_attempt", 10)),
        "escalate_user_attempt": int(cfg.get("escalate_user_attempt", 20)),
    }


def get_rework_max_attempts() -> int:
    return int(get_attempt_policy()["max_attempts"])


def next_loop_action(judged_attempt: int, policy: Optional[Dict[str, int]] = None) -> str:
    """After judging attempt N, what happens next (no silent skip of judgment N)."""
    pol = policy or get_attempt_policy()
    escalate_at = int(pol["escalate_user_attempt"])
    strategy_at = int(pol["strategy_change_attempt"])
    if judged_attempt >= escalate_at:
        return "escalate_user"
    # Attempt law: one change of approach (~10), then ordinary rework until ~20.
    if judged_attempt == strategy_at - 1:
        return "strategy_change"
    return "rework"


def build_rework_prompt(
    user_intent: str,
    prior_response: str,
    *,
    evidence_issues: Optional[List[str]] = None,
    quality_issues: str = "",
    command_to_aura: str = "",
    attempt: int = 1,
    max_attempts: int = 20,
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
    if command_to_aura:
        block.append(f"EVALUATOR COMMAND:\n{command_to_aura}")
    block.append(f"YOUR PRIOR RESPONSE:\n{(prior_response or '')[:2000]}")
    block.append(
        "Respond with a corrected user-facing answer. "
        "If impossible, state clearly that you cannot complete and why. "
        "End with facts JSON: ```json\n{\"facts\": {\"step_complete\": true}}\n```"
    )
    return "\n\n".join(block)


def build_strategy_change_prompt(
    user_intent: str,
    prior_response: str,
    *,
    evidence_issues: Optional[List[str]] = None,
    quality_issues: str = "",
    command_to_aura: str = "",
    attempt: int = 10,
    max_attempts: int = 20,
) -> str:
    base = build_rework_prompt(
        user_intent,
        prior_response,
        evidence_issues=evidence_issues,
        quality_issues=quality_issues,
        command_to_aura=command_to_aura,
        attempt=attempt,
        max_attempts=max_attempts,
    )
    return (
        "STRATEGY CHANGE — do not repeat the prior approach. "
        "Rebuild the plan, use different tools, wait on an external blocker, "
        "or ask the user one concrete question.\n\n" + base
    )


def build_escalation_message(
    user_intent: str,
    prior_response: str,
    *,
    evidence_issues: Optional[List[str]] = None,
    quality_issues: str = "",
    attempts: int = 20,
) -> str:
    issues = evidence_issues or []
    issue_txt = "; ".join(issues) if issues else (quality_issues or "the result still didn't meet the bar")
    return (
        f"I tried this {attempts} times and still couldn't finish it cleanly.\n\n"
        f"You asked: {(user_intent or '')[:500]}\n\n"
        f"What blocked it: {issue_txt[:800]}\n\n"
        f"Last attempt:\n{(prior_response or '')[:1200]}"
    )


def should_admit_failure(attempt: int, max_attempts: int, response: str) -> bool:
    """Explicit admission only — never skip evaluating attempt N just because N==max."""
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
