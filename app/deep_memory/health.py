"""Deep memory's health: readiness checks, invariants, and the status and report views.

Readiness reads the pipeline (ingest lag, enrichment, index drift, the memory lane, recall latency
and follow-up rate). Invariants read stored facts over the invariants' rolling window.
"""
from __future__ import annotations

import asyncio
import math
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from app.config import get_deep_memory_config
from app.db.database import AsyncSessionLocal
from app.db.models import (
    DeepChunk, DeepContextReport, DeepDocument, DeepIngestJob, DeepSection, Event, MemoryItem, Task, TaskMessage,
    VectorOutbox,
)
from app.production.readiness import CheckResult

WINDOW_HOURS = 24
INGEST_LAG_WARN_MIN = 15
INGEST_LAG_FAIL_MIN = 60
FAILING_ATTEMPTS = 3
ENRICH_WITHIN_MIN = 30
LANE_BUDGET_WARN = 0.8
RECALL_P95_TARGET_MS = 90_000
RECALL_FAILED_SHARE_WARN = 0.25
FOLLOWUP_RATE_WARN = 0.5
MIN_SAMPLES = 4


def _since(hours: int = WINDOW_HOURS) -> datetime:
    return datetime.utcnow() - timedelta(hours=hours)


def _p(values: List[int], q: float) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


async def ingest_stats() -> Dict[str, Any]:
    now = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        pending = DeepIngestJob.done_at.is_(None)
        oldest_due, due = (await db.execute(
            select(func.min(DeepIngestJob.next_attempt_at), func.count()).where(
                pending, DeepIngestJob.next_attempt_at <= now)
        )).one()
        total = (await db.execute(select(func.count()).where(pending))).scalar_one()
        failing = (await db.execute(
            select(DeepIngestJob.kind, func.count()).where(pending, DeepIngestJob.attempts >= FAILING_ATTEMPTS)
            .group_by(DeepIngestJob.kind)
        )).all()
        deferred = (await db.execute(
            select(func.count()).where(pending, DeepIngestJob.last_error.like("deferred:%"))
        )).scalar_one()
        documents = dict((await db.execute(
            select(DeepDocument.status, func.count()).where(DeepDocument.valid_to.is_(None))
            .group_by(DeepDocument.status)
        )).all())
        stale_raw = (await db.execute(
            select(func.count()).where(
                DeepDocument.valid_to.is_(None), DeepDocument.status == "raw",
                DeepDocument.updated_at < now - timedelta(minutes=ENRICH_WITHIN_MIN))
        )).scalar_one()
        chunks = (await db.execute(select(func.count()).where(DeepChunk.valid_to.is_(None)))).scalar_one()
        facts = (await db.execute(
            select(func.count()).where(MemoryItem.scope_type == "user", MemoryItem.valid_to.is_(None))
        )).scalar_one()
    return {
        "pending": total, "due": due,
        "oldest_due_minutes": round((now - oldest_due).total_seconds() / 60, 1) if oldest_due else 0.0,
        "failing": dict(failing), "deferred": deferred,
        "documents": documents, "documents_raw_overdue": stale_raw, "chunks": chunks, "facts": facts,
    }


async def index_stats() -> Dict[str, Any]:
    from app.deep_memory import index
    from app.memory.vector_sync import _deep_expected

    async with AsyncSessionLocal() as db:
        objects = len(await _deep_expected(db))
        outbox = (await db.execute(
            select(func.count()).where(VectorOutbox.kind == "deep", VectorOutbox.done_at.is_(None))
        )).scalar_one()
    stats: Dict[str, Any] = {"collection": index.collection_name(), "objects": objects, "outbox_pending": outbox}
    if not index.is_enabled():
        return {**stats, "enabled": False}
    exists = await asyncio.to_thread(index.collection_exists)
    points = await asyncio.to_thread(index.count_points) if exists else 0
    return {**stats, "enabled": True, "exists": exists, "points": points}


def lane_stats() -> Dict[str, Any]:
    from app.llm import usage_monitor
    from app.llm.openai_direct import USAGE_SOURCE, lane_policy
    from app.llm.quota_broker import get_orchestration_status

    budget = lane_policy().daily_token_budget
    used = usage_monitor.source_tokens_today(USAGE_SOURCE)
    return {"tokens_today": used, "budget": budget, "share": round(used / budget, 3) if budget else 1.0,
            **get_orchestration_status()["memory_lane"]}


async def recall_stats(hours: int = WINDOW_HOURS) -> Dict[str, Any]:
    """The task-start reports of the window: outcomes, latency of reports read, use, novelty."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(DeepContextReport.status, DeepContextReport.latency_ms, DeepContextReport.consumed_by,
                   DeepContextReport.novelty).where(
                DeepContextReport.trigger == "task_start", DeepContextReport.created_at >= _since(hours))
        )).all()
        followups = Counter(
            (e or {}).get("outcome") for e in (await db.execute(
                select(Event.event_payload).where(
                    Event.event_type == "deep_recall.followup", Event.occurred_at >= _since(hours))
            )).scalars()
        )
    read = [r.latency_ms for r in rows if r.status in ("ready", "empty") and r.latency_ms is not None]
    return {
        "reports": len(rows),
        "by_status": dict(Counter(r.status for r in rows)),
        "p50_ms": _p(read, 0.5), "p95_ms": _p(read, 0.95),
        "consumed_by": dict(Counter(r.consumed_by or "open" for r in rows)),
        "novelty": dict(Counter((r.novelty or {}).get("verdict") for r in rows if r.novelty)),
        "followups": {k: v for k, v in followups.items() if k},
    }


async def deep_memory_status() -> Dict[str, Any]:
    """Each part on its own: one that cannot be read reports its error instead."""
    async def part(read) -> Dict[str, Any]:
        try:
            return await read
        except Exception as exc:
            return {"error": str(exc)[:300]}

    cfg = get_deep_memory_config()
    ingest, index, lane, recall = await asyncio.gather(
        part(ingest_stats()), part(index_stats()), part(asyncio.to_thread(lane_stats)), part(recall_stats())
    )
    return {
        "switches": {k: bool(cfg.get(k)) for k in ("enabled", "recall_enabled", "followups_enabled")},
        "ingest": ingest, "index": index, "lane": lane, "recall_24h": recall,
    }


async def deep_memory_reports(task_id: str) -> List[Dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(DeepContextReport).where(DeepContextReport.task_id == task_id)
            .order_by(DeepContextReport.created_at)
        )).scalars().all()
    return [
        {"id": r.id, "trigger": r.trigger, "status": r.status, "queries": r.queries, "candidate_ids": r.candidate_ids,
         "report": r.report, "brief": r.brief, "latency_ms": r.latency_ms, "usage": r.usage,
         "consumed_by": r.consumed_by, "novelty": r.novelty,
         "created_at": r.created_at.isoformat() if r.created_at else None,
         "completed_at": r.completed_at.isoformat() if r.completed_at else None}
        for r in rows
    ]


# Readiness.

async def check_deep_memory_ingest() -> CheckResult:
    """The ingest queue drains: no due job waits long, and none keeps failing."""
    if not get_deep_memory_config().get("enabled"):
        return CheckResult("deep_memory_ingest", "pass", "Deep memory disabled")
    s = await ingest_stats()
    details = {k: s[k] for k in ("pending", "due", "oldest_due_minutes", "failing", "deferred")}
    lag = s["oldest_due_minutes"]
    if lag >= INGEST_LAG_FAIL_MIN:
        return CheckResult("deep_memory_ingest", "fail", f"Ingest stalled: a job has waited {lag:.0f} min", details)
    if lag >= INGEST_LAG_WARN_MIN or s["failing"]:
        return CheckResult("deep_memory_ingest", "warn",
                           f"Ingest lagging {lag:.0f} min; failing jobs {s['failing'] or 0}", details)
    return CheckResult("deep_memory_ingest", "pass", f"Ingest drains ({s['pending']} pending, lag {lag:.0f} min)",
                       details)


async def check_deep_memory_enrichment() -> CheckResult:
    """Documents get summaries and headers: no document stays raw long, no enrichment keeps failing."""
    if not get_deep_memory_config().get("enabled"):
        return CheckResult("deep_memory_enrichment", "pass", "Deep memory disabled")
    s = await ingest_stats()
    failing = (s["failing"] or {}).get("enrich", 0)
    details = {"documents": s["documents"], "raw_overdue": s["documents_raw_overdue"], "enrich_failing": failing,
               "deferred": s["deferred"]}
    if failing or s["documents_raw_overdue"]:
        return CheckResult(
            "deep_memory_enrichment", "warn",
            f"{s['documents_raw_overdue']} document(s) raw over {ENRICH_WITHIN_MIN} min, {failing} enrichment(s) failing"
            + (f", {s['deferred']} job(s) deferred to tomorrow's budget" if s["deferred"] else ""),
            details,
        )
    return CheckResult("deep_memory_enrichment", "pass", "Documents enriched on time", details)


async def check_deep_memory_index() -> CheckResult:
    """The hybrid collection exists and holds one point per object, outbox work aside."""
    s = await index_stats()
    if not s["enabled"]:
        return CheckResult("deep_memory_index", "pass", "Deep index disabled", s)
    if not s["exists"]:
        return CheckResult("deep_memory_index", "fail", f"Collection {s['collection']} missing", s)
    drift = abs(s["objects"] - s["points"])
    if drift > s["outbox_pending"]:
        return CheckResult("deep_memory_index", "warn",
                           f"{s['points']} points for {s['objects']} objects ({s['outbox_pending']} queued); "
                           "the nightly reconcile repairs drift", s)
    return CheckResult("deep_memory_index", "pass", f"{s['points']} points for {s['objects']} objects", s)


async def check_memory_lane() -> CheckResult:
    """The IA's direct model calls stay inside the day's token budget."""
    s = await asyncio.to_thread(lane_stats)
    if s["share"] >= 1:
        return CheckResult("memory_lane", "fail", f"Memory lane budget spent ({s['tokens_today']:,} tokens)", s)
    if s["share"] >= LANE_BUDGET_WARN:
        return CheckResult("memory_lane", "warn", f"Memory lane at {s['share']:.0%} of its daily budget", s)
    return CheckResult("memory_lane", "pass", f"Memory lane at {s['share']:.0%} of its daily budget", s)


async def check_deep_recall() -> CheckResult:
    """Recall latency and failures, and how often a recall leads to a follow-up."""
    if not get_deep_memory_config().get("recall_enabled"):
        return CheckResult("deep_recall", "pass", "Recall off")
    s = await recall_stats()
    failed = s["by_status"].get("failed", 0)
    ready = s["by_status"].get("ready", 0)
    sent = s["followups"].get("delivered", 0)
    problems = []
    if s["p95_ms"] is not None and s["p95_ms"] > RECALL_P95_TARGET_MS:
        problems.append(f"report p95 {s['p95_ms'] / 1000:.0f}s over {RECALL_P95_TARGET_MS // 1000}s")
    if s["reports"] >= MIN_SAMPLES and failed / s["reports"] >= RECALL_FAILED_SHARE_WARN:
        problems.append(f"{failed} of {s['reports']} recalls failed")
    if ready >= MIN_SAMPLES and sent / ready > FOLLOWUP_RATE_WARN:
        problems.append(f"follow-ups after {sent} of {ready} ready reports")
    if problems:
        return CheckResult("deep_recall", "warn", "; ".join(problems), s)
    p95 = "-" if s["p95_ms"] is None else f"{s['p95_ms'] / 1000:.0f}s"
    return CheckResult("deep_recall", "pass",
                       f"{s['reports']} recall(s) in {WINDOW_HOURS}h, p95 {p95}, {sent} follow-up(s)", s)


READINESS_CHECKS = (
    check_deep_memory_ingest, check_deep_memory_enrichment, check_deep_memory_index, check_memory_lane,
    check_deep_recall,
)


async def run_readiness_checks() -> List[CheckResult]:
    async def one(check) -> CheckResult:
        try:
            return await check()
        except Exception as exc:
            return CheckResult(check.__name__.removeprefix("check_"), "warn", f"Check could not run: {exc}", {})

    return list(await asyncio.gather(*(one(c) for c in READINESS_CHECKS)))


# Invariants.

async def check_task_documents() -> CheckResult:
    """Every user task that finished since go-live has an enriched task document within 30 minutes."""
    from app.deep_memory.ingest import document_id, ingestible, task_source_key
    from app.orchestrator.decision_engine import TERMINAL_STATUSES

    if not get_deep_memory_config().get("enabled"):
        return CheckResult("task_documents", "pass", "Deep memory disabled", {})
    now = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        live_since = (await db.execute(select(func.min(DeepIngestJob.created_at)))).scalar_one()
        if live_since is None:
            return CheckResult("task_documents", "pass", "Nothing ingested yet", {})
        tasks = (await db.execute(
            select(Task).where(
                Task.status.in_(TERMINAL_STATUSES), Task.created_at >= live_since,
                Task.updated_at >= _since(), Task.updated_at < now - timedelta(minutes=ENRICH_WITHIN_MIN))
        )).scalars().all()
        user = [t for t in tasks if ingestible(t)]
        enriched = set((await db.execute(
            select(DeepDocument.id).where(
                DeepDocument.id.in_([document_id(task_source_key(t.id)) for t in user]),
                DeepDocument.status == "enriched")
        )).scalars())
    missing = [t.id for t in user if document_id(task_source_key(t.id)) not in enriched]
    if missing:
        return CheckResult("task_documents", "fail",
                           f"{len(missing)} finished user task(s) lack an enriched task document after "
                           f"{ENRICH_WITHIN_MIN} min", {"task_ids": missing[:20]})
    return CheckResult("task_documents", "pass", f"All {len(user)} finished user task(s) have enriched documents", {})


async def check_claude_records() -> CheckResult:
    """Every user task that finished with Claude work has that work in its task document within 30 minutes."""
    from app.coding.records import has_records
    from app.deep_memory.ingest import document_id, ingestible, task_source_key
    from app.orchestrator.decision_engine import TERMINAL_STATUSES

    if not get_deep_memory_config().get("enabled"):
        return CheckResult("claude_records", "pass", "Deep memory disabled", {})
    now = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        tasks = (await db.execute(
            select(Task).where(
                Task.status.in_(TERMINAL_STATUSES), Task.updated_at >= _since(),
                Task.updated_at < now - timedelta(minutes=ENRICH_WITHIN_MIN))
        )).scalars().all()
        worked = [t for t in tasks if ingestible(t) and await asyncio.to_thread(has_records, t.id)]
        recorded = set((await db.execute(
            select(DeepSection.document_id).where(
                DeepSection.document_id.in_([document_id(task_source_key(t.id)) for t in worked]),
                DeepSection.path == "Claude sessions")
        )).scalars()) if worked else set()
    missing = [t.id for t in worked if document_id(task_source_key(t.id)) not in recorded]
    if missing:
        return CheckResult("claude_records", "fail",
                           f"{len(missing)} finished task(s) with Claude work lack it in their memory document after "
                           f"{ENRICH_WITHIN_MIN} min", {"task_ids": missing[:20]})
    return CheckResult("claude_records", "pass", f"All {len(worked)} finished task(s) with Claude work have it in memory", {})


async def check_deep_index_internal() -> CheckResult:
    """No content from canaries, heartbeats, cron runs, internal intents or intake placeholders in deep memory."""
    from app.deep_memory.ingest import ingestible

    async with AsyncSessionLocal() as db:
        documents = (await db.execute(
            select(DeepDocument.id, Task).join(Task, Task.id == DeepDocument.task_id)
            .where(DeepDocument.valid_to.is_(None))
        )).all()
    internal = sorted({task.id for _, task in documents if not ingestible(task)})
    if internal:
        return CheckResult("deep_index_internal", "fail",
                           f"Deep memory holds documents from {len(internal)} internal task(s)", {"task_ids": internal[:20]})
    return CheckResult("deep_index_internal", "pass", "No internal content in deep memory", {})


async def check_judged_followups() -> CheckResult:
    """Every follow-up Kirill received was accepted by the evaluator first."""
    async with AsyncSessionLocal() as db:
        followups = (await db.execute(
            select(TaskMessage.task_id, TaskMessage.meta, TaskMessage.created_at).where(
                TaskMessage.kind == "followup", TaskMessage.created_at >= _since())
        )).all()
        accepts = (await db.execute(
            select(Event.entity_id, Event.event_payload, Event.occurred_at).where(
                Event.event_type == "evaluator.accept", Event.entity_id.in_(sorted({f.task_id for f in followups})))
        )).all() if followups else []
    accepted = {(a.entity_id, (a.event_payload or {}).get("attempt")): a.occurred_at for a in accepts}
    unjudged = sorted({
        f.task_id for f in followups
        if not (when := accepted.get((f.task_id, (f.meta or {}).get("attempt")))) or when > f.created_at
    })
    if unjudged:
        return CheckResult("judged_followups", "fail",
                           f"{len(unjudged)} follow-up(s) sent without an evaluator accept", {"task_ids": unjudged[:20]})
    return CheckResult("judged_followups", "pass", f"All {len(followups)} follow-up(s) were accepted first", {})


INVARIANT_CHECKS = (check_task_documents, check_claude_records, check_deep_index_internal, check_judged_followups)
