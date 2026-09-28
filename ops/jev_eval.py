"""Validate labeled cases offline, or explicitly run paid Jev evaluation.

No database, workflow, memory, or Slack mutations are performed.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import math
from pathlib import Path
from statistics import mean
from app.decisions.jev import MODEL, Policy, close_jev_client, get_client
from app.decisions.memory import (
    MAX_PROMOTION_CANDIDATES, MAX_RERANK_CANDIDATES, PROMOTION_RUBRIC,
    RERANK_RUBRIC, build_promotion_request, build_rerank_request,
    promotion_decisions, ranking_order,
)
from app.memory.promotion import validate_fact


def ndcg(order: list[int], grades: list[int], k: int = 3) -> float:
    def dcg(indices):
        return sum((2 ** grades[i] - 1) / math.log2(rank + 2) for rank, i in enumerate(indices[:k]))
    best = dcg(sorted(range(len(grades)), key=lambda i: -grades[i]))
    return dcg(order) / best if best else 1.0


def load_cases(path: Path, max_cases: int) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not 0 < len(cases) <= max_cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("case limit exceeded, empty data, or duplicate ids")
    for c in cases:
        if c["operation"] == "rerank":
            rows, grades = c["rows"], c["grades"]
            if not 2 <= len(rows) <= MAX_RERANK_CANDIDATES or len(rows) != len(grades):
                raise ValueError("invalid rerank case")
            if not all(type(g) is int and 0 <= g <= 2 for g in grades):
                raise ValueError("grades must be integers from 0 to 2")
        elif c["operation"] == "promotion":
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


async def run(cases: list[dict], policy: Policy) -> dict:
    records = []
    try:
        for case in cases:
            is_rank = case["operation"] == "rerank"
            if is_rank:
                state, questions = build_rerank_request(case["query"], case["rows"])
            else:
                state, questions = build_promotion_request(case["source"], case["candidates"])
            ev = await get_client().evaluate(state, questions, purpose="eval." + case["id"],
                rubric=RERANK_RUBRIC if is_rank else PROMOTION_RUBRIC, policy=policy)
            record = {"id": case["id"], "operation": case["operation"], "status": ev.status,
                "reason": ev.reason, "latency_ms": ev.latency_ms, "request_hash": ev.request_hash,
                "input_tokens": ev.result["usage"]["input_tokens"] if ev.result else None}
            if is_rank:
                baseline = list(range(len(case["rows"])))
                proposed = ranking_order(ev.result["answers"], len(baseline), policy) if ev.result else None
                effective = proposed if proposed is not None else baseline
                record.update(order=effective, applied=proposed is not None,
                    baseline_ndcg3=ndcg(baseline, case["grades"]), jev_ndcg3=ndcg(effective, case["grades"]))
            else:
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
    promotions = [r for r in records if r["operation"] == "promotion"]
    ranks = [r for r in records if r["operation"] == "rerank"]
    accepted_count = sum(len(r["accepted"]) for r in promotions)
    errors = sum(r["false_promotions"] for r in promotions)
    tokens = sum(r["input_tokens"] or 0 for r in records)
    latencies = sorted(r["latency_ms"] for r in records)
    return {"model": MODEL, "live": True, "cases": len(records),
        "unavailable": sum(r["status"] != "ok" for r in records),
        "input_tokens_reported": tokens, "estimated_usd_for_reported_tokens": tokens / 1_000_000 * 0.042,
        "cost_note": "Published direct-provider input price; failed/unreported calls may also be billed.",
        "p50_ms": latencies[(len(latencies) - 1) // 2], "p95_ms": latencies[math.ceil(0.95 * len(latencies)) - 1],
        "promotion": {"accepted": accepted_count, "false_promotions": errors,
            "false_holds": sum(r["false_holds"] for r in promotions),
            "baseline_false_promotions": sum(r["baseline_false_promotions"] for r in promotions),
            "precision_on_accepted": 1 - errors / accepted_count if accepted_count else None,
            "zero_error_upper95": 1 - 0.05 ** (1 / accepted_count) if accepted_count and errors == 0 else None},
        "rerank": {"baseline_ndcg3": mean(r["baseline_ndcg3"] for r in ranks) if ranks else None,
            "jev_ndcg3": mean(r["jev_ndcg3"] for r in ranks) if ranks else None}, "records": records,
        "scope": "Labeled input comparison only; not an end-to-end Aura or original-LLM benchmark."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path(__file__).resolve().parents[1] / "tests/fixtures/jev_memory_eval.jsonl")
    parser.add_argument("--max-cases", type=int, default=20)
    parser.add_argument("--live", action="store_true", help="Send dataset text to TypeSafe; consumes API credits.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = load_cases(args.dataset, args.max_cases)
    result = asyncio.run(run(cases, Policy(cache_ttl_sec=0))) if args.live else {
        "live": False, "validated_cases": len(cases), "model": MODEL,
        "message": "Dataset structure validated. No model called; no accuracy or latency measured."}
    rendered = json.dumps(result, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
