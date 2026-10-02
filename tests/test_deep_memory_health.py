"""Deep memory's readiness checks, invariants and status views, on a real SQLite schema."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base, DeepContextReport, DeepDocument, DeepIngestJob, Event, Task, TaskMessage, VectorOutbox,
)
from app.deep_memory import health, index
from app.deep_memory.ingest import document_id, task_source_key
from app.memory import vector_sync

NOW = datetime.utcnow()


@pytest.fixture
async def sessions(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'health.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for module in (health, vector_sync):
        monkeypatch.setattr(module, "AsyncSessionLocal", maker)
    yield maker
    await engine.dispose()


def switches(monkeypatch, **on):
    cfg = {"enabled": True, "recall_enabled": False, "followups_enabled": False, **on}
    monkeypatch.setattr(health, "get_deep_memory_config", lambda: cfg)


async def add(maker, *rows):
    async with maker() as db:
        db.add_all(rows)
        await db.commit()


def job(minutes_due=0, **kw):
    return DeepIngestJob(kind=kw.pop("kind", "task"), ref_id=kw.pop("ref_id", "t1"),
                         next_attempt_at=NOW - timedelta(minutes=minutes_due), created_at=NOW - timedelta(hours=2), **kw)


async def test_ingest_lag_warns_then_fails(sessions, monkeypatch):
    switches(monkeypatch)
    assert (await health.check_deep_memory_ingest()).status == "pass"
    await add(sessions, job(minutes_due=20))
    result = await health.check_deep_memory_ingest()
    assert result.status == "warn" and result.details["due"] == 1
    await add(sessions, job(minutes_due=70, ref_id="t2"))
    assert (await health.check_deep_memory_ingest()).status == "fail"


async def test_a_job_that_keeps_failing_warns_even_without_lag(sessions, monkeypatch):
    switches(monkeypatch)
    await add(sessions, DeepIngestJob(kind="enrich", ref_id="d1", attempts=3, next_attempt_at=NOW + timedelta(minutes=30)))
    result = await health.check_deep_memory_ingest()
    assert result.status == "warn" and result.details["failing"] == {"enrich": 1}
    enrichment = await health.check_deep_memory_enrichment()
    assert enrichment.status == "warn" and enrichment.details["enrich_failing"] == 1


async def test_a_document_left_raw_past_half_an_hour_warns(sessions, monkeypatch):
    switches(monkeypatch)
    await add(sessions, DeepDocument(id="d1", kind="task", source_key="task:t1", status="raw",
                                     updated_at=NOW - timedelta(minutes=10)))
    assert (await health.check_deep_memory_enrichment()).status == "pass"
    await add(sessions, DeepDocument(id="d2", kind="task", source_key="task:t2", status="raw",
                                     updated_at=NOW - timedelta(minutes=45)))
    result = await health.check_deep_memory_enrichment()
    assert result.status == "warn" and result.details["raw_overdue"] == 1


async def test_index_drift_beyond_the_outbox_warns_and_a_missing_collection_fails(sessions, monkeypatch):
    await add(sessions, DeepDocument(id="d1", kind="task", source_key="task:t1", summary="s", status="enriched"))
    monkeypatch.setattr(index, "is_enabled", lambda: True)
    monkeypatch.setattr(index, "collection_exists", lambda: True)
    monkeypatch.setattr(index, "count_points", lambda: 1)
    assert (await health.check_deep_memory_index()).status == "pass"
    monkeypatch.setattr(index, "count_points", lambda: 0)
    assert (await health.check_deep_memory_index()).status == "warn"
    await add(sessions, VectorOutbox(kind="deep", ref_id="document:d1"))
    assert (await health.check_deep_memory_index()).status == "pass"
    monkeypatch.setattr(index, "collection_exists", lambda: False)
    assert (await health.check_deep_memory_index()).status == "fail"


@pytest.mark.parametrize("used,status", [(100_000, "pass"), (3_300_000, "warn"), (4_000_000, "fail")])
async def test_the_memory_lane_budget(monkeypatch, used, status):
    from app.llm import quota_broker, usage_monitor

    monkeypatch.setattr(usage_monitor, "source_tokens_today", lambda source: used)
    monkeypatch.setattr(quota_broker, "get_orchestration_status", lambda: {"memory_lane": {"active": 0}})
    result = await health.check_memory_lane()
    assert result.status == status and result.details["budget"] == 4_000_000


def report(status="ready", latency=20_000, consumed="evaluator", novelty=None, trigger="task_start", **kw):
    return DeepContextReport(task_id=kw.pop("task_id", "t1"), trigger=trigger, status=status, latency_ms=latency,
                             consumed_by=consumed, novelty=novelty, created_at=NOW - timedelta(hours=1))


async def test_recall_latency_failures_and_follow_up_rate(sessions, monkeypatch):
    switches(monkeypatch)
    assert (await health.check_deep_recall()).message == "Recall off"
    switches(monkeypatch, recall_enabled=True)
    await add(sessions, *[report() for _ in range(4)], report(trigger="probe", latency=500_000))
    ok = await health.check_deep_recall()
    assert ok.status == "pass" and ok.details["p95_ms"] == 20_000 and ok.details["reports"] == 4
    await add(sessions, report(latency=200_000), report(status="failed", latency=None, consumed="none"),
              report(status="failed", latency=None, consumed="none"))
    slow = await health.check_deep_recall()
    assert slow.status == "warn" and "p95 200s" in slow.message and "2 of 7 recalls failed" in slow.message


async def test_a_follow_up_after_most_ready_reports_warns(sessions, monkeypatch):
    switches(monkeypatch, recall_enabled=True)
    await add(sessions, *[report(consumed="followup", novelty={"verdict": "adds"}) for _ in range(3)], report(),
              *[Event(entity_id=f"t{i}", event_type="deep_recall.followup", event_payload={"outcome": "delivered"})
                for i in range(3)])
    result = await health.check_deep_recall()
    assert result.status == "warn" and "follow-ups after 3 of 4 ready reports" in result.message
    assert result.details["novelty"] == {"adds": 3} and result.details["followups"] == {"delivered": 3}


def task(task_id, *, status="completed", ended_minutes=40, created_minutes=90, **kw):
    return Task(id=task_id, goal=kw.pop("goal", "Plan a day in Kobe."), task_type=kw.pop("task_type", "user"),
                status=status, created_at=NOW - timedelta(minutes=created_minutes),
                updated_at=NOW - timedelta(minutes=ended_minutes), **kw)


async def test_every_finished_user_task_needs_an_enriched_document_after_30_minutes(sessions, monkeypatch):
    switches(monkeypatch)
    await add(sessions, job(ref_id="t-first"),
              task("t-ok"), task("t-raw"), task("t-none"), task("t-fresh", ended_minutes=10),
              task("t-canary", task_type="canary", goal="RMP CANARY: Reply with exactly CANARY_OK"),
              task("t-before", created_minutes=60 * 5),
              DeepDocument(id=document_id(task_source_key("t-ok")), kind="task", source_key="task:t-ok", status="enriched"),
              DeepDocument(id=document_id(task_source_key("t-raw")), kind="task", source_key="task:t-raw", status="raw"))
    result = await health.check_task_documents()
    assert result.status == "fail" and sorted(result.details["task_ids"]) == ["t-none", "t-raw"]


async def test_no_internal_task_has_a_deep_memory_document(sessions):
    await add(sessions, task("t-user"), task("t-canary", task_type="canary", goal="RMP CANARY: Reply with CANARY_OK"),
              DeepDocument(id="d1", kind="task", source_key="task:t-user", task_id="t-user"))
    assert (await health.check_deep_index_internal()).status == "pass"
    await add(sessions, DeepDocument(id="d2", kind="task", source_key="task:t-canary", task_id="t-canary"))
    result = await health.check_deep_index_internal()
    assert result.status == "fail" and result.details["task_ids"] == ["t-canary"]


async def test_every_follow_up_needs_the_accept_of_its_own_verdict_first(sessions):
    def followup(task_id, attempt, at):
        return TaskMessage(id=f"m-{task_id}", task_id=task_id, role="assistant", kind="followup", content="more",
                           meta={"attempt": attempt}, created_at=NOW - timedelta(minutes=at))

    def accept(task_id, attempt, at):
        return Event(entity_id=task_id, event_type="evaluator.accept", event_payload={"attempt": attempt},
                     occurred_at=NOW - timedelta(minutes=at))

    await add(sessions, followup("t-ok", 2, 5), accept("t-ok", 1, 20), accept("t-ok", 2, 6))
    assert (await health.check_judged_followups()).status == "pass"
    await add(sessions, followup("t-other-attempt", 3, 5), accept("t-other-attempt", 2, 6),
              followup("t-late", 2, 5), accept("t-late", 2, 1))
    result = await health.check_judged_followups()
    assert result.status == "fail" and result.details["task_ids"] == ["t-late", "t-other-attempt"]


async def test_the_status_and_report_views(sessions, monkeypatch):
    from app.api import server
    from app.llm import quota_broker, usage_monitor

    switches(monkeypatch, recall_enabled=True)
    monkeypatch.setattr(index, "is_enabled", lambda: False)
    monkeypatch.setattr(usage_monitor, "source_tokens_today", lambda source: 1000)
    monkeypatch.setattr(quota_broker, "get_orchestration_status", lambda: {"memory_lane": {"active": 1}})
    await add(sessions, job(minutes_due=2), report(task_id="t9", novelty={"verdict": "none"}))
    status = await server.deep_memory_status()
    assert status["switches"] == {"enabled": True, "recall_enabled": True, "followups_enabled": False}
    assert status["ingest"]["due"] == 1 and status["index"]["enabled"] is False
    assert status["lane"]["tokens_today"] == 1000 and status["lane"]["active"] == 1
    assert status["recall_24h"]["reports"] == 1 and status["recall_24h"]["novelty"] == {"none": 1}
    reports = await server.deep_memory_reports("t9")
    assert reports["task_id"] == "t9" and [r["status"] for r in reports["reports"]] == ["ready"]
    assert (await server.deep_memory_reports("nobody"))["reports"] == []


async def test_a_part_that_cannot_be_read_reports_its_error(sessions, monkeypatch):
    switches(monkeypatch)

    def down():
        raise RuntimeError("usage ledger unreadable")

    monkeypatch.setattr(health, "lane_stats", down)
    monkeypatch.setattr(index, "is_enabled", lambda: False)
    status = await health.deep_memory_status()
    assert status["lane"] == {"error": "usage ledger unreadable"} and "pending" in status["ingest"]
