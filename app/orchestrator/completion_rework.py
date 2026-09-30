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


# A rework starts in a fresh session, so its brief carries the whole request and draft.
REQUEST_CHARS = 8000
DRAFT_CHARS = 40000


def build_rework_prompt(
    user_intent: str,
    prior_response: str,
    *,
    evidence_issues: Optional[List[str]] = None,
    quality_issues: str = "",
    command_to_aura: str = "",
    attempt: int = 1,
    max_attempts: int = 20,
    memory_block: str = "",
    actions: str = "",
) -> str:
    """The rework brief: request, memory, the evaluator's issues, actions already taken, the draft."""
    issues = evidence_issues or []
    block = [
        "COMPLETION REJECTED — revise your answer or admit you cannot complete.",
        f"Attempt {attempt} of {max_attempts}. This is a fresh session: everything you need is below.",
        f"ORIGINAL REQUEST:\n{user_intent[:REQUEST_CHARS]}",
    ]
    if memory_block.strip():
        block.append(memory_block.strip())
    if issues:
        block.append("EVIDENCE ISSUES:\n- " + "\n- ".join(issues))
    if quality_issues:
        block.append(f"QUALITY ISSUES:\n{quality_issues}")
    if command_to_aura:
        block.append(f"EVALUATOR COMMAND:\n{command_to_aura}")
    if actions.strip():
        block.append(
            "ACTIONS ALREADY TAKEN IN THIS TASK (reuse what succeeded; do not redo it):\n" + actions.strip()
        )
    block.append(f"YOUR PRIOR RESPONSE:\n{(prior_response or '')[:DRAFT_CHARS]}")
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
    memory_block: str = "",
    actions: str = "",
) -> str:
    base = build_rework_prompt(
        user_intent,
        prior_response,
        evidence_issues=evidence_issues,
        quality_issues=quality_issues,
        command_to_aura=command_to_aura,
        attempt=attempt,
        max_attempts=max_attempts,
        memory_block=memory_block,
        actions=actions,
    )
    return (
        "STRATEGY CHANGE — do not repeat the prior approach. "
        "Rebuild the plan, use different tools, wait on an external blocker, "
        "or ask the user one concrete question.\n\n" + base
    )


# Program-owned: what Kirill hears when deep recall finds more after Aura's reply.
RECALL_NOTICE = (
    "I recalled some more information from our earlier conversations. I need a little time to work "
    "it into a more accurate answer, and I'll get back to you."
)
RECALL_UNCONFIRMED_NOTICE = "I couldn't confirm the refined answer, so my earlier reply stands."
EARLIER_REPLY_BRIEF_CHARS = 12000


def build_recall_refinement_prompt(
    user_intent: str,
    earlier_reply: str,
    *,
    verdict: str,
    points: List[str],
    memory_block: str = "",
    actions: str = "",
) -> str:
    """The refinement brief (session __recall): request, the reply sent, what memory changes, memory, actions."""
    block = [
        "REFINE YOUR ANSWER FROM MEMORY. This is a fresh session: everything you need is below.",
        f"ORIGINAL REQUEST:\n{user_intent[:REQUEST_CHARS]}",
        f"YOUR REPLY (already sent to Kirill):\n{(earlier_reply or '')[:DRAFT_CHARS]}",
        ("WHAT YOUR MEMORY CORRECTS:\n- " if verdict == "corrects" else "WHAT YOUR MEMORY ADDS:\n- ")
        + "\n- ".join(points),
    ]
    if memory_block.strip():
        block.append(memory_block.strip())
    if actions.strip():
        block.append(
            "ACTIONS ALREADY TAKEN IN THIS TASK (reuse what succeeded; do not redo it):\n" + actions.strip()
        )
    block.append(
        "Kirill has been told you recalled more and are working it in. Write your refined answer: say "
        "briefly what you recalled, then give the whole corrected answer if your reply was short, or only "
        "the parts that change if it was long. Do not repeat what stays the same. "
        "End with facts JSON: ```json\n{\"facts\": {\"step_complete\": true}}\n```"
    )
    return "\n\n".join(block)


def followup_brief(earlier_reply: str, verdict: str) -> str:
    """What the evaluator needs to judge a follow-up rather than a first answer."""
    change = "a correction" if verdict == "corrects" else "more"
    return (
        f"FOLLOW-UP: Aura's reply below was already sent. Memory then showed that it needed {change}; "
        "AURA OUTPUT is her follow-up. Judge it as a follow-up to that reply: it must be right about what "
        "memory says, and it need not repeat what the reply already said.\n"
        f"EARLIER REPLY (sent):\n{(earlier_reply or '')[:EARLIER_REPLY_BRIEF_CHARS]}"
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
