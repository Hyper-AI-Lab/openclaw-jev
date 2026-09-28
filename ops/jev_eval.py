"""Validate labeled cases offline, or explicitly run paid Jev evaluation.

Memory promotion (default) or intake (--intake). No database, workflow, memory, or Slack
mutations are performed.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from app.decisions.intake import INTAKE_RUBRIC, build_intake_request, compose_intake_result
from app.decisions.jev import MAX_REQUEST_BYTES, MODEL, Policy, close_jev_client, get_client
from app.decisions.memory import (
    MAX_PROMOTION_CANDIDATES, PROMOTION_RUBRIC, build_promotion_request, promotion_decisions,
)
from app.memory.promotion import validate_fact

ROOT = Path(__file__).resolve().parents[1]
EVAL_NOW = datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc)
EVAL_SESSION = "agent:main:slack:channel:eval"
EVAL_POLICY = Policy(cache_ttl_sec=0, requests_per_minute=120)
# Spacing that keeps a sequential run under the per-process limit above.
MIN_REQUEST_SPACING_SEC = 60 / EVAL_POLICY.requests_per_minute
RUNNING_DECISIONS = frozenset({"attach_active", "wait_active", "rebuild_stale"})
INTAKE_DECISIONS = RUNNING_DECISIONS | {"create_fresh", "create_guided", "clarify", "abstain"}
EXECUTION_MODES = frozenset({"conversational", "structured_work"})
# Enforce only when a live run meets every bound.
INTAKE_GATE = {"max_harmful": 0, "min_accuracy_on_accepted": 0.9, "min_coverage": 0.5, "max_p95_ms": 1500}


def load_cases(path: Path, max_cases: int) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not 0 < len(cases) <= max_cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("case limit exceeded, empty data, or duplicate ids")
    for c in cases:
        if c["operation"] == "promotion":
            facts, labels = c["candidates"], c["allow"]
            if not 1 <= len(facts) <= MAX_PROMOTION_CANDIDATES or len(labels) != len(facts):
                raise ValueError("invalid promotion case")
            if not all(type(label) is bool for label in labels):
                raise ValueError("promotion labels must be Boolean")
            if not all(validate_fact(f)[0] for f in facts):
                raise ValueError("fixtures must first pass deterministic gates")
        else:
            raise ValueError("unknown operation")
    return cases


async def _evaluate_paced(state: dict, questions: dict, *, purpose: str, rubric: str, policy: Policy):
    started = asyncio.get_running_loop().time()
    ev = await get_client().evaluate(state, questions, purpose=purpose, rubric=rubric, policy=policy)
    await asyncio.sleep(max(0.0, MIN_REQUEST_SPACING_SEC - (asyncio.get_running_loop().time() - started)))
    return ev


async def run(cases: list[dict], policy: Policy) -> dict:
    records = []
    try:
        for case in cases:
            state, questions = build_promotion_request(case["source"], case["candidates"])
            ev = await _evaluate_paced(state, questions, purpose="eval." + case["id"],
                rubric=PROMOTION_RUBRIC, policy=policy)
            record = {"id": case["id"], "operation": case["operation"], "status": ev.status,
                "reason": ev.reason, "latency_ms": ev.latency_ms, "request_hash": ev.request_hash,
                "input_tokens": ev.result["usage"]["input_tokens"] if ev.result else None}
            accepted, decisions = promotion_decisions(ev.result["answers"], len(case["candidates"]), policy) if ev.result else ([], [])
            labels = case["allow"]
            record.update(accepted=accepted, decisions=decisions,
                baseline_false_promotions=sum(not x for x in labels),
                false_promotions=sum(not labels[i] for i in accepted),
                false_holds=sum(label and i not in accepted for i, label in enumerate(labels)),
                eligible_candidates=len(labels), labeled_positive=sum(labels))
            records.append(record)
    finally:
        await close_jev_client()
    accepted_count = sum(len(r["accepted"]) for r in records)
    errors = sum(r["false_promotions"] for r in records)
    return {"model": MODEL, "live": True, "cases": len(records),
        "unavailable": sum(r["status"] != "ok" for r in records), **_usage(records),
        "promotion": {"accepted": accepted_count, "false_promotions": errors,
            "false_holds": sum(r["false_holds"] for r in records),
            "baseline_false_promotions": sum(r["baseline_false_promotions"] for r in records),
            "precision_on_accepted": 1 - errors / accepted_count if accepted_count else None,
            "zero_error_upper95": 1 - 0.05 ** (1 / accepted_count) if accepted_count and errors == 0 else None},
        "records": records,
        "scope": "Labeled input comparison only; not an end-to-end Aura or original-LLM benchmark."}


def _usage(records: list[dict]) -> dict:
    tokens = sum(r["input_tokens"] or 0 for r in records)
    latencies = sorted(r["latency_ms"] for r in records)
    return {"input_tokens_reported": tokens, "estimated_usd_for_reported_tokens": tokens / 1_000_000 * 0.042,
        "cost_note": "Published direct-provider input price; failed/unreported calls may also be billed.",
        "p50_ms": latencies[(len(latencies) - 1) // 2], "p95_ms": latencies[math.ceil(0.95 * len(latencies)) - 1]}


def intake_context(case: dict) -> tuple[dict, list[str]]:
    """Rebuild the intake context a case describes, with synthetic task ids."""
    active = [{"task_id": f"{case['id']}:R{i}", "status": t.get("status", "running"), "session_key": EVAL_SESSION,
        "task_kind": "one_shot", "goal": t["goal"], "goal_snippet": t["goal"][:300],
        "updated_at": (EVAL_NOW - timedelta(minutes=t.get("minutes_ago", 5))).replace(tzinfo=None).isoformat()}
        for i, t in enumerate(case.get("running") or [], 1)]
    registry = [{"task_id": f"{case['id']}:F{i}", "terminal_status": f.get("result", "completed"),
        "process_type": "user", "intent_snippet": f["request"], "outcome_summary": f.get("outcome", ""),
        "task_ended_at": (EVAL_NOW - timedelta(days=f.get("days_ago", 1))).replace(tzinfo=None).isoformat()}
        for i, f in enumerate(case.get("finished") or [], 1)]
    context = {"intent": case["message"], "session_key": EVAL_SESSION, "active_tasks": active,
        "recent_registry": registry, "memory_hits": [{"snippet": m} for m in case.get("memory") or []]}
    return context, list(case.get("dialogue") or [])


def _labels(value) -> list:
    """An expected alias may be one label or a list of equally correct labels."""
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def load_intake_cases(paths: list[Path], max_cases: int) -> tuple[list[dict], int]:
    """Return (labeled cases, unlabeled count); raise on malformed data."""
    from app.workflows.catalog import CATALOG

    cases = [json.loads(line) for p in paths for line in p.read_text().splitlines() if line.strip()]
    if not 0 < len(cases) <= max_cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("case limit exceeded, empty data, or duplicate ids")
    labeled = []
    for c in cases:
        if c.get("operation") != "intake" or not str(c.get("message") or "").strip():
            raise ValueError(f"{c.get('id')}: not an intake case")
        context, dialogue = intake_context(c)
        state, questions, aliases = build_intake_request(context, dialogue, now=EVAL_NOW)
        size = len(json.dumps({"model": MODEL, "state": state, "questions": questions}, ensure_ascii=False).encode())
        if size > MAX_REQUEST_BYTES:
            raise ValueError(f"{c['id']}: request exceeds {MAX_REQUEST_BYTES} bytes")
        exp = c.get("expect")
        if exp is None:
            continue
        if not exp.get("decision") or set(exp["decision"]) - INTAKE_DECISIONS:
            raise ValueError(f"{c['id']}: unknown expected decision")
        for key, prefix in (("target", "R"), ("similar", "F")):
            for alias in _labels(exp.get(key)):
                if not alias.startswith(prefix) or alias not in aliases:
                    raise ValueError(f"{c['id']}: expected {key} {alias} is not a listed task")
        if exp.get("execution_mode") is not None and exp["execution_mode"] not in EXECUTION_MODES:
            raise ValueError(f"{c['id']}: unknown execution mode")
        if exp.get("catalog") is not None and set(exp["catalog"]) - (set(CATALOG) | {"none"}):
            raise ValueError(f"{c['id']}: unknown catalog label")
        labeled.append(c)
    return labeled, len(cases) - len(labeled)


def score_intake(case: dict, proposal: dict | None, aliases: dict) -> dict:
    """Harmful: an accepted attach/wait/rebuild on the wrong target, or a wrong catalog template."""
    exp = case["expect"]
    if proposal is None:
        return {"accepted": False, "correct": None, "harmful": False, "errors": []}
    back = {task_id: alias for alias, task_id in aliases.items()}
    decision = proposal["decision"]
    target = back.get(proposal["target_task_id"]) if proposal["target_task_id"] else None
    similar = back.get(proposal["similar_task_ids"][0]) if proposal["relation_class"] == "finished" else None
    errors = []
    if decision not in exp["decision"]:
        errors.append("decision")
    if decision in RUNNING_DECISIONS and target != exp.get("target"):
        errors.append("target")
    if exp.get("similar") and decision == "create_guided" and similar not in _labels(exp["similar"]):
        errors.append("similar")
    if exp.get("execution_mode") and proposal["execution_mode"] != exp["execution_mode"]:
        errors.append("execution_mode")
    if exp.get("catalog") is not None and (proposal["catalog_hint"] or "none") not in exp["catalog"]:
        errors.append("catalog")
    harmful = (decision in RUNNING_DECISIONS and bool({"decision", "target"} & set(errors))) or (
        proposal["catalog_hint"] is not None and "catalog" in errors)
    return {"accepted": True, "correct": not errors, "harmful": harmful, "errors": errors,
        "decision": decision, "target": target, "catalog": proposal["catalog_hint"]}


async def run_intake(cases: list[dict], policy: Policy) -> dict:
    records = []
    try:
        for case in cases:
            context, dialogue = intake_context(case)
            state, questions, aliases = build_intake_request(context, dialogue, now=EVAL_NOW)
            ev = await _evaluate_paced(state, questions, purpose="eval." + case["id"],
                rubric=INTAKE_RUBRIC, policy=policy)
            answers = ev.result["answers"] if ev.status == "ok" else {}
            proposal = compose_intake_result(answers, aliases, policy) if answers else None
            records.append({"id": case["id"], "source": case.get("source", "synthetic"), "status": ev.status,
                "reason": ev.reason, "latency_ms": ev.latency_ms,
                "input_tokens": ev.result["usage"]["input_tokens"] if ev.result else None,
                "answers": {qid: [a["choice"], round(a["confidence"], 3)] for qid, a in answers.items()},
                **score_intake(case, proposal, aliases)})
    finally:
        await close_jev_client()
    accepted = [r for r in records if r["accepted"]]
    correct = sum(bool(r["correct"]) for r in accepted)
    harmful = sum(r["harmful"] for r in records)
    abstain_ids = {c["id"] for c in cases if c["expect"]["decision"] == ["abstain"]}
    usage = _usage(records)
    accuracy = correct / len(accepted) if accepted else None
    coverage = len(accepted) / len(records)
    gate = {**INTAKE_GATE, "passed": harmful <= INTAKE_GATE["max_harmful"] and accuracy is not None
        and accuracy >= INTAKE_GATE["min_accuracy_on_accepted"] and coverage >= INTAKE_GATE["min_coverage"]
        and usage["p95_ms"] < INTAKE_GATE["max_p95_ms"]}
    return {"model": MODEL, "live": True, "rubric": INTAKE_RUBRIC, "cases": len(records),
        "unavailable": sum(r["status"] != "ok" for r in records), **usage,
        "intake": {"accepted": len(accepted), "coverage": coverage, "correct_on_accepted": correct,
            "accuracy_on_accepted": accuracy, "harmful_errors": harmful,
            "abstain_expected": len(abstain_ids),
            "abstained_when_expected": sum(not r["accepted"] for r in records if r["id"] in abstain_ids)},
        "gate": gate, "records": records,
        "scope": "Labeled input comparison only; not an end-to-end Aura or original-LLM benchmark."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intake", action="store_true", help="Evaluate intake decisions instead of memory promotion.")
    parser.add_argument("--dataset", type=Path, action="append",
        help="JSONL cases; repeat to combine. Defaults to the committed synthetic fixtures.")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--live", action="store_true", help="Send dataset text to TypeSafe; consumes API credits.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.intake:
        paths = args.dataset or [ROOT / "tests/fixtures/jev_intake_eval.jsonl"]
        cases, unlabeled = load_intake_cases(paths, args.max_cases or 250)
        result = asyncio.run(run_intake(cases, EVAL_POLICY)) if args.live else {
            "live": False, "validated_cases": len(cases), "unlabeled_skipped": unlabeled, "model": MODEL,
            "message": "Dataset structure validated. No model called; no accuracy or latency measured."}
        if args.live:
            result["unlabeled_skipped"] = unlabeled
    else:
        path = (args.dataset or [ROOT / "tests/fixtures/jev_memory_eval.jsonl"])[0]
        cases = load_cases(path, args.max_cases or 20)
        result = asyncio.run(run(cases, EVAL_POLICY)) if args.live else {
            "live": False, "validated_cases": len(cases), "model": MODEL,
            "message": "Dataset structure validated. No model called; no accuracy or latency measured."}
    rendered = json.dumps(result, indent=2, allow_nan=False, ensure_ascii=False)
    if args.output:
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
