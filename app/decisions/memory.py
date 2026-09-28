"""Aura Jev rubrics: semantic advice, never effect authorization."""
from __future__ import annotations

import hashlib
import json
import logging
from app.decisions.jev import MODEL, Policy, choice, get_client, get_policy

logger = logging.getLogger("rmp.jev.memory")
PROMOTION_RUBRIC = "aura.memory-promotion.v1"
MAX_PROMOTION_CANDIDATES = 8


def build_promotion_request(source: str, candidates: list[dict]) -> tuple[dict, dict]:
    state = {"episode": source, "candidates": [
        {"text": str(f.get("content") or ""), "kind": str(f.get("kind") or "")} for f in candidates]}
    questions = {}
    for i in range(len(candidates)):
        questions[f"support_{i}"] = choice(
            f"Is the entire claim in state.candidates[{i}].text explicitly supported by state.episode? "
            "A plan, attempted action, or ambiguous status does not establish success. "
            "Evaluate what the episode says, not whether it is true outside this evidence.",
            {"supported": "The full claim is explicitly stated in the episode.",
             "unsupported": "At least part of the claim is absent, contradicted, or merely planned.",
             "unknown": "Evidence is ambiguous or incomplete."})
        questions[f"durability_{i}"] = choice(
            f"Should state.candidates[{i}].text remain useful across future tasks?",
            {"durable": "A stable preference, reusable rule or enduring fact.",
             "transient": "A temporary login/session status, one-off outcome or merely a referenced URL.",
             "unknown": "Its future usefulness cannot be established."})
        questions[f"scope_{i}"] = choice(
            f"Does state.episode explicitly establish state.candidates[{i}].text as applying to this user across tasks? "
            "Do not generalize a website policy, another person's preference or a single process constraint to the user.",
            {"user": "Explicit user-level preference or fact applicable across tasks.",
             "local": "Applies only to a site, example, third party, session or individual process.",
             "unknown": "User-level applicability is not explicit."})
    return state, questions


def promotion_decisions(answers: dict, count: int, policy: Policy) -> tuple[list[int], list[dict]]:
    accepted, records = [], []
    for i in range(count):
        selected = [answers[f"{name}_{i}"] for name in ("support", "durability", "scope")]
        passes = all(a["choice"] == expected and a["choice_is_max"]
            and a["confidence"] >= policy.promotion_min_confidence
            and a["probabilities"][expected] >= policy.promotion_min_probability
            for a, expected in zip(selected, ("supported", "durable", "user")))
        if passes:
            accepted.append(i)
        records.append({"candidate": i, "would_allow": passes, "answers": selected})
    return accepted, records


async def review_promotions(source: str, candidates: list[dict], *, scope_key: str) -> dict:
    """Enforce holds uncertain semantic/pinned promotions; shadow preserves baseline."""
    policy = get_policy()
    report = {"mode": policy.promotion_mode, "allowed_indices": list(range(len(candidates))),
        "held_indices": [], "reason": "disabled", "model": None, "rubric": PROMOTION_RUBRIC}
    if policy.promotion_mode == "off" or not candidates:
        return report
    accepted, records, reason = [], [], "candidate_limit"
    if len(candidates) <= MAX_PROMOTION_CANDIDATES:
        purpose = "memory.promote." + hashlib.sha256(scope_key.encode()).hexdigest()[:16]
        state, questions = build_promotion_request(source, candidates)
        ev = await get_client().evaluate(state, questions, purpose=purpose, rubric=PROMOTION_RUBRIC, policy=policy)
        reason = ev.reason or "reviewed"
        report["request_hash"] = ev.request_hash
        if ev.status == "ok":
            report["model"] = MODEL
            accepted, records = promotion_decisions(ev.result["answers"], len(candidates), policy)
    report.update(held_indices=[i for i in range(len(candidates)) if i not in accepted], reason=reason, evaluations=records)
    if policy.promotion_mode == "enforce":
        report["allowed_indices"] = accepted
    logger.info("jev promotion %s", json.dumps(report, sort_keys=True))
    return report
