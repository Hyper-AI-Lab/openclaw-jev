"""Deep-memory enrichment (stage 2) with the model mocked, plus the registry fixes it feeds."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base, DeepChunk, DeepDocument, DeepIngestJob, DeepSection, Task, TaskMessage, TaskRegistryEntry, VectorOutbox,
)
from app.deep_memory import enrich, ingest
from app.llm.openai_direct import DirectBudgetExceeded
from app.task_registry import retriever, summary

T0 = datetime(2026, 9, 30, 5, 8)
DOC = ingest.document_id("task:t1")


@pytest.fixture
async def sessions(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'enrich.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for module in (enrich, ingest, summary, retriever):
        monkeypatch.setattr(module, "AsyncSessionLocal", maker)
    yield maker
    await engine.dispose()


class FakeModel:
    """Answers each schema from its input; records every call."""

    def __init__(self, drop_context=False):
        self.calls = []
        self.drop_context = drop_context

    async def __call__(self, schema, *, purpose, instructions, input_text, priority, max_output_tokens):
        self.calls.append((purpose, input_text))
        if schema is enrich.SectionEnrichment:
            count = input_text.count("\n[") + input_text.startswith("[")
            numbers = [n for n in range(count) if f"[{n}] " in input_text]
            if self.drop_context:
                numbers = numbers[:-1]
            value = schema(summary=f"Summary of {purpose} with {len(numbers)} chunks.",
                           contexts=[{"chunk": n, "context": f"Context for chunk {n}."} for n in numbers])
        elif schema is enrich.TaskEnrichment:
            value = schema(summary="Kirill asked for a Kobe day plan; Aura gave one.",
                           outcome="Answered with a one-day Kobe itinerary.",
                           answer="Harborland in the morning, Kobe beef at Mouriya for lunch, Kitano in the afternoon.")
        else:
            value = schema(summary="A page about Mouriya's lunch sets.")
        return SimpleNamespace(value=value)


async def seed_task_document(maker, *, chunk_count=2, kind="task", sections=("Conversation", "Path history")):
    async with maker() as db:
        db.add(Task(id="t1", goal="Plan a day in Kobe.", task_type="user", status="completed", created_at=T0,
                    updated_at=T0 + timedelta(minutes=3), openclaw_session_key="s1"))
        db.add(TaskMessage(id="m9", task_id="t1", role="assistant", kind="reply", created_at=T0 + timedelta(minutes=2),
                           content="Harborland, then Kobe beef at Mouriya, then Kitano."))
        doc = DeepDocument(id=DOC, kind=kind, source_key=f"{kind}:t1", title="Plan a day in Kobe.", task_id="t1",
                           session_key="s1", source_at=T0, source_ref={"url": "https://mouriya.co.jp", "untrusted": True}
                           if kind == "tool_document" else None,
                           toc=[{"section_id": f"s{i}", "ordinal": i, "path": name, "title": name}
                                for i, name in enumerate(sections)])
        db.add(doc)
        ordinal = 0
        for i, name in enumerate(sections):
            db.add(DeepSection(id=f"s{i}", document_id=DOC, ordinal=i, path=name, title=name))
            for n in range(chunk_count):
                db.add(DeepChunk(id=f"c{i}-{n}", document_id=DOC, section_id=f"s{i}", ordinal=ordinal,
                                 text=f"{name} chunk {n}: Kobe beef at lunch."))
                ordinal += 1
        await db.commit()


async def run_enrich(maker):
    async with maker() as db:
        outcome = await enrich.enrich_document(db, DOC)
        await db.commit()
    return outcome


async def all_rows(maker, model):
    async with maker() as db:
        return list((await db.execute(select(model))).scalars().all())


async def test_a_task_document_gets_summaries_headers_outcome_and_answer_and_is_reindexed(sessions, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(enrich, "structured_call", model)
    await seed_task_document(sessions)
    assert await run_enrich(sessions) == "done"
    assert [p for p, _ in model.calls] == ["deep_memory.section", "deep_memory.section", "deep_memory.task"]
    chunks = await all_rows(sessions, DeepChunk)
    assert all(c.context_header.startswith("Context for chunk") for c in chunks)
    sections = {s.id: s for s in await all_rows(sessions, DeepSection)}
    assert sections["s0"].summary.startswith("Summary of deep_memory.section")
    [doc] = await all_rows(sessions, DeepDocument)
    assert doc.status == "enriched" and doc.enriched_at
    assert doc.summary == ("Kirill asked for a Kobe day plan; Aura gave one. Outcome: Answered with a one-day Kobe "
                           "itinerary. Answer: Harborland in the morning, Kobe beef at Mouriya for lunch, Kitano in "
                           "the afternoon.")
    assert doc.meta["answer"].startswith("Harborland") and [e["summary"] for e in doc.toc][0].startswith("Summary")
    task_input = model.calls[-1][1]
    assert "KIRILL ASKED:\nPlan a day in Kobe." in task_input and "Mouriya, then Kitano" in task_input
    refs = sorted((o.kind, o.ref_id) for o in await all_rows(sessions, VectorOutbox))
    assert refs == sorted([("deep", f"chunk:c{i}-{n}") for i in range(2) for n in range(2)]
                          + [("deep", "section:s0"), ("deep", "section:s1"), ("deep", f"document:{DOC}"),
                             ("registry", "t1")])


async def test_a_reply_logged_before_message_kinds_is_still_the_final_reply(sessions):
    await seed_task_document(sessions)
    async with sessions() as db:
        (await db.get(TaskMessage, "m9")).kind = None
        db.add(TaskMessage(id="m10", task_id="t1", role="assistant", kind="notice", content="Got it.",
                           created_at=T0 + timedelta(minutes=2, seconds=30)))
        await db.commit()
        assert await enrich._final_reply(db, "t1") == "Harborland, then Kobe beef at Mouriya, then Kitano."


async def test_enriching_again_calls_no_model_and_changes_nothing(sessions, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(enrich, "structured_call", model)
    await seed_task_document(sessions)
    await run_enrich(sessions)
    calls, outbox = len(model.calls), len(await all_rows(sessions, VectorOutbox))
    assert await run_enrich(sessions) == "done"
    assert len(model.calls) == calls and len(await all_rows(sessions, VectorOutbox)) == outbox


async def test_a_rewritten_chunk_is_enriched_again_with_its_section_and_document(sessions, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(enrich, "structured_call", model)
    await seed_task_document(sessions)
    await run_enrich(sessions)
    async with sessions() as db:
        chunk = await db.get(DeepChunk, "c1-0")
        chunk.text, chunk.context_header = "Path history chunk 0: rework asked for the restaurant.", None
        await db.commit()
    model.calls.clear()
    await run_enrich(sessions)
    assert [p for p, _ in model.calls] == ["deep_memory.section", "deep_memory.task"]
    assert "rework asked for the restaurant" in model.calls[0][1]


async def test_an_answer_missing_a_chunk_writes_nothing_so_the_job_retries(sessions, monkeypatch):
    monkeypatch.setattr(enrich, "structured_call", FakeModel(drop_context=True))
    await seed_task_document(sessions)
    with pytest.raises(ValueError, match="got contexts for"):
        await run_enrich(sessions)
    assert all(c.context_header is None for c in await all_rows(sessions, DeepChunk))
    assert (await all_rows(sessions, DeepDocument))[0].status == "raw"


async def test_a_chunk_rewritten_during_the_model_call_keeps_no_stale_header(sessions, monkeypatch):
    model = FakeModel()

    async def rewrite_then_answer(schema, **kwargs):
        if schema is enrich.SectionEnrichment and "Conversation" in kwargs["input_text"].split("SECTION: ")[1][:20]:
            async with sessions() as db:
                (await db.get(DeepChunk, "c0-1")).text = "Conversation chunk 1: changed meanwhile."
                await db.commit()
        return await model(schema, **kwargs)

    monkeypatch.setattr(enrich, "structured_call", rewrite_then_answer)
    await seed_task_document(sessions)
    await run_enrich(sessions)
    headers = {c.id: c.context_header for c in await all_rows(sessions, DeepChunk)}
    assert headers["c0-1"] is None and headers["c0-0"] and headers["c1-0"]


async def test_a_single_section_document_takes_its_summary_from_that_section(sessions, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(enrich, "structured_call", model)
    await seed_task_document(sessions, kind="tool_document", sections=("Mouriya lunch",))
    await run_enrich(sessions)
    assert [p for p, _ in model.calls] == ["deep_memory.section"]
    assert "TRUST: external web content" in model.calls[0][1] and "SOURCE: https://mouriya.co.jp" in model.calls[0][1]
    assert "READ OR WRITTEN DURING THE TASK: Plan a day in Kobe." in model.calls[0][1]
    [doc] = await all_rows(sessions, DeepDocument)
    assert doc.summary.startswith("Summary of deep_memory.section")
    assert ("registry", "t1") not in {(o.kind, o.ref_id) for o in await all_rows(sessions, VectorOutbox)}


async def test_a_long_section_is_enriched_in_batches_the_model_can_answer_whole(sessions, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(enrich, "structured_call", model)
    monkeypatch.setattr(enrich, "CHUNKS_PER_CALL", 3)
    await seed_task_document(sessions, chunk_count=7, kind="deliverable", sections=("Guide",))
    await run_enrich(sessions)
    assert [p for p, _ in model.calls] == ["deep_memory.section"] * 3
    assert all(c.context_header for c in await all_rows(sessions, DeepChunk))
    section = (await all_rows(sessions, DeepSection))[0]
    assert section.summary.count("Summary of") == 3


async def test_a_spent_daily_budget_defers_the_job_to_the_next_day_without_counting_a_failure(sessions, monkeypatch):
    async def spent(*args, **kwargs):
        raise DirectBudgetExceeded("daily_budget", "4,000,000 of 4,000,000 tokens used today")

    monkeypatch.setattr(enrich, "structured_call", spent)
    await seed_task_document(sessions)
    async with sessions() as db:
        await ingest.enqueue(db, "enrich", DOC)
        await db.commit()
    await ingest.ingest_once()
    [job] = await all_rows(sessions, DeepIngestJob)
    assert job.attempts == 0 and job.done_at is None and job.last_error.startswith("deferred: daily_budget")
    tomorrow = datetime.utcnow().date() + timedelta(days=1)
    assert job.next_attempt_at.date() == tomorrow


async def test_the_registry_leads_with_the_outcome_and_the_answer(sessions, monkeypatch):
    monkeypatch.setattr(enrich, "structured_call", FakeModel())
    await seed_task_document(sessions)
    before = await summary.build_task_summary("t1")
    assert before["outcome_summary"].startswith("status=completed")
    await run_enrich(sessions)
    after = await summary.build_task_summary("t1")
    assert after["outcome_summary"].startswith(
        "Outcome: Answered with a one-day Kobe itinerary. Answer: Harborland in the morning"
    ) and after["outcome_summary"].endswith("[status=completed]")


async def test_recent_registry_means_recently_finished_and_never_internal(sessions):
    now = datetime.utcnow()
    async with sessions() as db:
        for tid, ended, indexed, ptype in (
            ("old-reindexed", now - timedelta(days=200), now, "user"),
            ("recent", now - timedelta(days=2), now - timedelta(days=2), "user"),
            ("canary", now - timedelta(hours=1), now, "canary"),
        ):
            db.add(Task(id=tid, goal=tid, task_type=ptype))
            db.add(TaskRegistryEntry(id=f"e-{tid}", task_id=tid, process_type=ptype, terminal_status="completed",
                                     task_ended_at=ended, indexed_at=indexed))
        await db.commit()
    assert [r["task_id"] for r in await retriever.fetch_recent_registry(days=90)] == ["recent"]
