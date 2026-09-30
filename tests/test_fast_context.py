"""Aura's fast context: one block, priority order, budgets, relevance floor, fallbacks, deadline."""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db.models import (
    Base, DeepDocument, Event, MemoryItem, Task, TaskIntakeDecision, TaskMessage, TaskRegistryEntry,
)
from app.deep_memory import curator, index, ingest
from app.memory import router
from app.task_registry import messages

SESSION = "agent:main:slack:channel:d0test"
T0 = datetime(2026, 9, 30, 5, 8)


@pytest.fixture
async def sessions(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fast.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for module in (curator, index, router, messages):
        monkeypatch.setattr(module, "AsyncSessionLocal", maker)
    monkeypatch.setattr(index, "is_enabled", lambda: True)
    monkeypatch.setattr(index, "collection_exists", lambda: True)
    monkeypatch.setattr(router, "is_vector_memory_enabled", lambda: False)
    yield maker
    await engine.dispose()


def fact_hit(text, when="2026-09-30T05:08:00Z"):
    return index.Hit(id=text[:8], score=0.5, payload={"text": text, "source_at": when, "level": "fact"})


async def seed(maker, *rows):
    async with maker() as db:
        db.add_all(rows)
        await db.commit()


async def assemble(query="What's my test code word?", task_id="t2", run="r2"):
    return await curator.assemble_fast_context(task_id=task_id, process_run_id=run, process_type="user", query=query)


async def test_one_block_in_priority_order_with_the_relevant_facts(sessions, monkeypatch):
    calls = []

    def search(query, **kwargs):
        calls.append(kwargs)
        return [fact_hit("Kirill's test code word is LYNX-44 (changed from ORCA-19).")]

    monkeypatch.setattr(index, "search", search)
    await seed(
        sessions,
        Task(id="t1", goal="Remember my code word", task_type="user", openclaw_session_key=SESSION, created_at=T0),
        TaskMessage(id="m1", task_id="t1", role="user", kind="request", content="Remember: my code word is LYNX-44.",
                    created_at=T0),
        TaskMessage(id="m2", task_id="t1", role="assistant", kind="reply", content="Noted: LYNX-44.",
                    created_at=T0 + timedelta(seconds=5)),
        Task(id="t2", goal="What's my test code word?", task_type="user", openclaw_session_key=SESSION,
             created_at=T0 + timedelta(minutes=5)),
        MemoryItem(id="w1", scope_type="process", scope_id="r2", memory_type="working", content="Checked the notes."),
    )
    block = await assemble()
    assert block.startswith(curator.HEADER) and block.count("PROCESS-SCOPED MEMORY") == 1
    order = [block.index(h) for h in ("RECENT DIALOGUE", "FACTS FROM EARLIER", "THIS TASK SO FAR")]
    assert order == sorted(order)
    assert "Kirill: Remember: my code word is LYNX-44." in block and "Aura: Noted: LYNX-44." in block
    assert "- [2026-09-30] Kirill's test code word is LYNX-44" in block and "[working] Checked the notes." in block
    assert calls == [{"levels": ("fact",), "match": {"scope_id": "default"}, "limit": 8, "dense_floor": 0.30}]
    async with sessions() as db:
        [event] = (await db.execute(select(Event).where(Event.event_type == "memory.fast_context"))).scalars().all()
    assert event.entity_id == "t2" and event.event_payload["sections"] == ["dialogue", "facts", "run"]


async def test_the_index_down_means_text_search_and_nothing_found_means_no_facts_section(sessions, monkeypatch):
    def down(*args, **kwargs):
        raise RuntimeError("qdrant unreachable")

    fallback = []

    async def fts(query, **kwargs):
        fallback.append(kwargs)
        return [fact_hit("Kirill lives in Kobe.")]

    monkeypatch.setattr(index, "search", down)
    monkeypatch.setattr(index, "fts_search", fts)
    await seed(sessions, Task(id="t2", goal="Where do I live?", task_type="user", openclaw_session_key=SESSION))
    assert "Kirill lives in Kobe." in await assemble("Where do I live?")
    assert fallback == [{"levels": ("fact",), "match": {"scope_id": "default"}, "limit": 3}]
    monkeypatch.setattr(index, "search", lambda *a, **k: [])
    assert "FACTS FROM EARLIER" not in await assemble("Where do I live?")


async def test_linked_tasks_bring_their_summary_or_else_their_registry_entry(sessions, monkeypatch):
    monkeypatch.setattr(index, "search", lambda *a, **k: [])
    await seed(
        sessions,
        Task(id="t0", goal="Guide to a home Kubernetes cluster", task_type="user", status="completed", created_at=T0),
        Task(id="t1", goal="Compare rail passes", task_type="user", status="completed", created_at=T0),
        Task(id="c1", goal="RMP CANARY: Reply with exactly CANARY_OK", task_type="canary", status="completed"),
        DeepDocument(id=ingest.document_id("task:t0"), kind="task", source_key="task:t0", task_id="t0",
                     summary="Aura delivered a 17-section K3s guide. Answer: three nodes, embedded etcd."),
        TaskRegistryEntry(id="e1", task_id="t1", outcome_summary="Outcome: JR Pass not worth it for Kansai."),
        TaskIntakeDecision(id="d2", decision="create_guided", similar_task_ids=["t0", "t1", "c1"]),
        Task(id="t2", goal="Add storage to my cluster", task_type="user", openclaw_session_key=SESSION,
             intake_decision_id="d2"),
    )
    block = await assemble("Add storage to my cluster")
    assert "EARLIER TASKS THIS ONE RELATES TO" in block
    assert "Answer: three nodes, embedded etcd." in block and "JR Pass not worth it" in block
    assert "CANARY" not in block


async def test_canaries_get_only_their_own_run(sessions, monkeypatch):
    monkeypatch.setattr(index, "search", lambda *a, **k: pytest.fail("no fact search for internal runs"))
    await seed(
        sessions,
        Task(id="t1", goal="Earlier user chat", task_type="user", openclaw_session_key=SESSION, created_at=T0),
        TaskMessage(id="m1", task_id="t1", role="user", kind="request", content="hello", created_at=T0),
        Task(id="c2", goal="RMP MEMORY CANARY: Reply with one sentence", task_type="canary",
             openclaw_session_key=SESSION),
        MemoryItem(id="w1", scope_type="process", scope_id="r2", memory_type="working", content="Canary fact X."),
    )
    block = await assemble("RMP MEMORY CANARY", task_id="c2")
    assert "Canary fact X." in block and "RECENT DIALOGUE" not in block
    empty = await assemble("RMP MEMORY CANARY", task_id="c2", run="r-none")
    assert empty == curator.EMPTY


async def test_budgets_cut_low_priority_sections_and_the_dialogue_keeps_its_newest_turns(sessions, monkeypatch):
    monkeypatch.setattr(index, "search", lambda *a, **k: [fact_hit(f"Fact number {i} about Kirill.") for i in range(8)])
    monkeypatch.setattr(config, "get_deep_memory_config",
                        lambda: {**config.DEFAULT_DEEP_MEMORY, "fast_context_max_chars": 3000})
    monkeypatch.setattr(curator, "get_deep_memory_config",
                        lambda: {**config.DEFAULT_DEEP_MEMORY, "fast_context_max_chars": 3000})
    rows = [Task(id="t1", goal="long chat", task_type="user", openclaw_session_key=SESSION, created_at=T0)]
    for n in range(40):
        rows.append(TaskMessage(id=f"m{n}", task_id="t1", role="user" if n % 2 == 0 else "assistant",
                                kind="request" if n % 2 == 0 else "reply", content=f"turn {n} " + "x" * 150,
                                created_at=T0 + timedelta(seconds=n)))
    rows.append(Task(id="t2", goal="continue", task_type="user", openclaw_session_key=SESSION,
                     created_at=T0 + timedelta(minutes=5)))
    rows += [MemoryItem(id=f"w{n}", scope_type="process", scope_id="r2", memory_type="working",
                        content=f"step note {n} " + "y" * 250) for n in range(8)]
    await seed(sessions, *rows)
    block = await assemble("continue")
    assert len(block) <= 3000 + 2
    assert "turn 39" in block and "turn 0 " not in block, "the dialogue keeps its newest turns"
    assert block.index("RECENT DIALOGUE") < block.index("FACTS FROM EARLIER")
    assert "THIS TASK SO FAR" not in block, "the lowest-priority section that does not fit is left out"


async def test_a_leg_over_the_deadline_is_left_out_and_the_rest_arrive(sessions, monkeypatch):
    def slow(*args, **kwargs):
        import time

        time.sleep(2.0)
        return [fact_hit("late fact")]

    monkeypatch.setattr(index, "search", slow)
    # SQLite legs under disk pressure take a few hundred ms; the deadline leaves them room.
    monkeypatch.setattr(curator, "get_deep_memory_config",
                        lambda: {**config.DEFAULT_DEEP_MEMORY, "fast_context_deadline_sec": 0.8})
    await seed(
        sessions,
        Task(id="t2", goal="Where do I live?", task_type="user", openclaw_session_key=SESSION),
        MemoryItem(id="w1", scope_type="process", scope_id="r2", memory_type="working", content="Run note."),
    )
    started = asyncio.get_running_loop().time()
    block = await assemble("Where do I live?")
    assert asyncio.get_running_loop().time() - started < 1.6
    assert "Run note." in block and "late fact" not in block


def test_fit_keeps_whole_lines():
    block = "HEAD\n" + "\n".join(f"line {i} " + "z" * 40 for i in range(10))
    assert curator._fit(block, 120).split("\n")[0] == "HEAD" and len(curator._fit(block, 120)) <= 120
    kept = curator._fit(block, 120, keep_end=True)
    assert kept.startswith("HEAD\n") and kept.endswith("line 9 " + "z" * 40)
