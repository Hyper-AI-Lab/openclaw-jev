"""Facts from conversations: extraction filters, consolidation, retirement, Jev review, citations."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, MemoryItem, MemoryLink, Task, TaskMessage, VectorOutbox
from app.deep_memory import facts, index, ingest
from app.memory import router

T0 = datetime(2026, 9, 30, 5, 8)


@pytest.fixture
async def sessions(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'facts.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for module in (facts, ingest, index, router):
        monkeypatch.setattr(module, "AsyncSessionLocal", maker)
    monkeypatch.setattr(router, "is_vector_memory_enabled", lambda: True)
    yield maker
    await engine.dispose()


class Model:
    """Scripted IA answers: extraction per task in order, consolidation as given."""

    def __init__(self, extractions, consolidations=()):
        self.extractions, self.consolidations = list(extractions), list(consolidations)
        self.calls = []

    async def __call__(self, schema, *, purpose, instructions, input_text, priority, max_output_tokens):
        self.calls.append((purpose, input_text))
        if schema is facts.FactExtraction:
            return SimpleNamespace(value=schema(facts=self.extractions.pop(0)))
        return SimpleNamespace(value=schema(decisions=self.consolidations.pop(0)))


def fact(statement, **kw):
    return {"statement": statement, "subject": kw.pop("subject", "test code word"), "kind": kw.pop("kind", "personal"),
            "said_by": kw.pop("said_by", "kirill"), "turns": kw.pop("turns", [0]), "valid_from": None,
            "confidence": kw.pop("confidence", 0.95)}


async def seed_task(maker, task_id, said, *, reply=True, at=0, task_type="user"):
    async with maker() as db:
        db.add(Task(id=task_id, goal=said, task_type=task_type, status="completed", created_at=T0,
                    openclaw_session_key="s1"))
        db.add(TaskMessage(id=f"{task_id}-u", task_id=task_id, role="user", kind="request", content=said,
                           created_at=T0 + timedelta(minutes=at)))
        if reply:
            db.add(TaskMessage(id=f"{task_id}-a", task_id=task_id, role="assistant", kind="reply", content="Noted.",
                               created_at=T0 + timedelta(minutes=at, seconds=5)))
        await db.commit()


async def run(maker, task_id):
    async with maker() as db:
        outcome = await facts.extract_task_facts(db, task_id)
        await db.commit()
    return outcome


async def active_facts(maker):
    async with maker() as db:
        return list((await db.execute(
            select(MemoryItem).where(MemoryItem.scope_type == "user", MemoryItem.valid_to.is_(None))
        )).scalars().all())


@pytest.fixture
def near(monkeypatch):
    """Neighbour search over the facts in the database, as the index would answer."""
    async def neighbours(statement):
        async with facts.AsyncSessionLocal() as db:
            rows = (await db.execute(select(MemoryItem.id).where(MemoryItem.scope_type == "user",
                                                                 MemoryItem.valid_to.is_(None)))).scalars().all()
        return list(rows)

    monkeypatch.setattr(facts, "_neighbours", neighbours)


async def test_a_changed_code_word_leaves_exactly_one_active_fact(sessions, monkeypatch, near):
    model = Model(
        [[fact("Kirill's test code word is PELICAN-47.")], [fact("Kirill's test code word is HERON-12.")]],
        [[{"candidate": 0, "action": "update", "existing": 0,
           "statement": "Kirill's test code word is HERON-12 (changed from PELICAN-47 on 2026-09-30)."}]],
    )
    monkeypatch.setattr(facts, "structured_call", model)
    await seed_task(sessions, "t1", "Remember this for later: my test code word is PELICAN-47.")
    assert await run(sessions, "t1") == "done"
    [first] = await active_facts(sessions)
    assert first.content == "Kirill's test code word is PELICAN-47." and first.confidence == 95
    assert first.provenance_ref["chunk_ids"] == [facts._turn_chunk_id("t1", "t1-u")]
    assert first.provenance_ref["subject"] == "test code word" and first.valid_from == T0

    await seed_task(sessions, "t2", "My test code word is HERON-12 now.", at=60)
    assert await run(sessions, "t2") == "done"
    [current] = await active_facts(sessions)
    assert current.content.startswith("Kirill's test code word is HERON-12") and current.supersedes_memory_id == first.id
    async with sessions() as db:
        old = await db.get(MemoryItem, first.id)
        links = (await db.execute(select(MemoryLink))).scalars().all()
        outbox = [o.ref_id for o in (await db.execute(select(VectorOutbox))).scalars().all()]
    assert old.valid_to is not None
    assert [(l.source_id, l.target_id, l.relation) for l in links] == [(current.id, first.id, "supersedes")]
    # The new fact is indexed and the retired one is queued so the index drops it.
    assert outbox.count(f"fact:{first.id}") == 2 and f"fact:{current.id}" in outbox
    consolidation = model.calls[-1][1]
    assert "[C0] (kirill) Kirill's test code word is HERON-12." in consolidation
    assert "[E0] (since 2026-09-30) Kirill's test code word is PELICAN-47." in consolidation


async def test_citations_are_the_chunks_stage_one_writes(sessions):
    await seed_task(sessions, "t1", "My test code word is PELICAN-47.")
    async with sessions() as db:
        await ingest.ingest_turn(db, "t1-u")
        await db.commit()
        from app.db.models import DeepChunk

        [chunk] = (await db.execute(select(DeepChunk))).scalars().all()
    assert chunk.id == facts._turn_chunk_id("t1", "t1-u")


async def test_the_same_fact_again_writes_nothing_and_a_contradiction_keeps_both(sessions, monkeypatch, near):
    model = Model(
        [[fact("Kirill lives in Kobe.", subject="home city")], [fact("Kirill lives in Kobe.", subject="home city")],
         [fact("Kirill lives in Osaka.", subject="home city", said_by="aura")]],
        [[{"candidate": 0, "action": "same", "existing": 0, "statement": "Kirill lives in Kobe."}],
         [{"candidate": 0, "action": "contradicts", "existing": 0, "statement": "Kirill lives in Osaka."}]],
    )
    monkeypatch.setattr(facts, "structured_call", model)
    for n, said in enumerate(("I live in Kobe.", "Kobe is home.", "Book a hotel near my home in Osaka.")):
        await seed_task(sessions, f"t{n}", said, at=n)
        await run(sessions, f"t{n}")
    current = sorted(f.content for f in await active_facts(sessions))
    assert current == ["Kirill lives in Kobe.", "Kirill lives in Osaka."]
    async with sessions() as db:
        [link] = (await db.execute(select(MemoryLink))).scalars().all()
    assert link.relation == "contradicts"


async def test_credentials_and_weak_facts_never_reach_review_or_memory(sessions, monkeypatch):
    model = Model([[
        fact("Kirill's test code word is PELICAN-47."),
        fact("Kirill's bank password is hunter2.", subject="bank password"),
        fact("Kirill's API key is sk-abcdefghijklmnopqrstuvwx1234.", subject="api key"),
        fact("Kirill's phone PIN is 4821.", subject="phone pin"),
        fact("Kirill might like jazz.", subject="music", confidence=0.4),
    ]])
    reviewed = []

    async def review(source, candidates, *, scope_key):
        reviewed.extend(c["content"] for c in candidates)
        return {"mode": "shadow", "allowed_indices": list(range(len(candidates))), "held_indices": []}

    monkeypatch.setattr(facts, "structured_call", model)
    monkeypatch.setattr("app.decisions.memory.review_promotions", review)
    monkeypatch.setattr(facts, "_neighbours", lambda statement: _none())
    await seed_task(sessions, "t1", "Remember my code word PELICAN-47, my bank password hunter2 and my PIN 4821.")
    await run(sessions, "t1")
    assert reviewed == ["Kirill's test code word is PELICAN-47."]
    assert [f.content for f in await active_facts(sessions)] == ["Kirill's test code word is PELICAN-47."]


async def _none():
    return []


async def test_jev_enforce_holds_a_fact_before_anything_is_written(sessions, monkeypatch):
    monkeypatch.setattr(facts, "structured_call", Model([[fact("Kirill prefers metric units.", subject="units")]]))
    monkeypatch.setattr(facts, "_neighbours", lambda statement: _none())

    async def hold(source, candidates, *, scope_key):
        return {"mode": "enforce", "allowed_indices": [], "held_indices": [0]}

    monkeypatch.setattr("app.decisions.memory.review_promotions", hold)
    await seed_task(sessions, "t1", "Always use metric units in all my future tasks.")
    assert await run(sessions, "t1") == "done"
    assert await active_facts(sessions) == []


async def test_nothing_is_extracted_without_a_delivered_reply_or_from_internal_runs(sessions, monkeypatch):
    model = Model([])
    monkeypatch.setattr(facts, "structured_call", model)
    await seed_task(sessions, "t1", "My code word is PELICAN-47.", reply=False)
    await seed_task(sessions, "c1", "RMP CANARY: Reply with exactly CANARY_OK", task_type="canary")
    assert await run(sessions, "t1") == "no delivered reply"
    assert await run(sessions, "c1") == "internal"
    assert model.calls == []


async def test_a_consolidation_answer_that_points_nowhere_is_treated_as_new(sessions, monkeypatch, near):
    model = Model(
        [[fact("Kirill's test code word is PELICAN-47.")], [fact("Kirill's partner is called Aiko.", subject="partner")]],
        [[{"candidate": 0, "action": "update", "existing": 7, "statement": "Kirill's partner is called Aiko."}]],
    )
    monkeypatch.setattr(facts, "structured_call", model)
    await seed_task(sessions, "t1", "My test code word is PELICAN-47.")
    await run(sessions, "t1")
    await seed_task(sessions, "t2", "My partner is Aiko.", at=5)
    await run(sessions, "t2")
    assert len(await active_facts(sessions)) == 2


def test_credential_detection_spares_ordinary_facts():
    assert facts.is_credential("Kirill's online banking password is hunter2.")
    assert facts.is_credential("password: hunter2")
    assert not facts.is_credential("Kirill's test code word is PELICAN-47.")
    assert not facts.is_credential("Kirill wants Aura to pin the itinerary message.")
