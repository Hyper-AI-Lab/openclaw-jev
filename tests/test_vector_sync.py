"""Postgres and Qdrant back each other up: outbox, drain, reconcile, full-text fallback."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.deep_memory import index
from app.memory import router, vector_sync
from app.memory.vector_sync import memory_point_payload

ROW = "11111111-1111-4111-8111-111111111111"


def item(scope_type="user", scope_id="default", memory_type="semantic", valid_to=None, provenance=None):
    return SimpleNamespace(id=ROW, scope_type=scope_type, scope_id=scope_id, memory_type=memory_type,
                           content="Kirill prefers metric units.", created_at=datetime(2026, 9, 29),
                           provenance_ref=provenance or {"task_id": "t1"}, valid_to=valid_to)


def test_points_carry_the_payload_mem0_searches():
    user = memory_point_payload(item())
    assert user["data"] == "Kirill prefers metric units." and user["user_id"] == "default"
    assert user["provenance"] == {"task_id": "t1", "memory_id": ROW} and user["memory_type"] == "semantic"
    proc = memory_point_payload(item(scope_type="procedural", scope_id="user", memory_type="procedural"))
    assert proc["agent_id"] == "procedural" and proc["procedural_scope_id"] == "user"
    assert memory_point_payload(item(scope_type="process", scope_id="pr1", memory_type="episodic"))["run_id"] == "pr1"


def _session(db):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=db)
    s.__aexit__ = AsyncMock(return_value=False)
    return lambda: s


async def test_a_memory_row_and_its_outbox_row_commit_together():
    added = []
    db = MagicMock()
    db.add = added.append
    db.commit = AsyncMock()
    with patch.object(router, "AsyncSessionLocal", _session(db)), \
         patch.object(router, "is_vector_memory_enabled", return_value=True):
        fact = await router.MemoryRouter.write("user", "default", "semantic", "Kirill prefers metric units.")
        episode = await router.MemoryRouter.write("process", "pr1", "episodic", "Step one found three trains.")
        await router.MemoryRouter.write("process", "pr1", "working", "scratch note for this step")
    kinds = [type(o).__name__ for o in added]
    assert kinds == ["MemoryItem", "VectorOutbox", "MemoryItem", "VectorOutbox", "MemoryItem"]
    # User memory is indexed in the deep-memory collection; process memory in the legacy one.
    assert (added[1].kind, added[1].ref_id) == ("deep", f"fact:{fact}")
    assert (added[3].kind, added[3].ref_id) == ("memory", episode)
    db.commit.assert_awaited()


def _outbox(kind="memory", ref=ROW):
    return SimpleNamespace(kind=kind, ref_id=ref, attempts=0, last_error=None,
                           next_attempt_at=datetime(2026, 9, 29), done_at=None)


async def _drain(rows, db_get, *, deep_enabled=True, statements=None):
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows

    async def execute(statement):
        if statements is not None:
            compiled = statement.compile()
            statements.append((str(compiled), dict(compiled.params)))
        return result

    db.execute = execute
    db.get = AsyncMock(side_effect=db_get)
    db.commit = AsyncMock()
    with patch.object(vector_sync, "AsyncSessionLocal", _session(db)), \
         patch.object(index, "is_enabled", return_value=deep_enabled):
        return await vector_sync.drain_once()


async def test_the_drain_indexes_retries_with_backoff_and_deletes_gone_rows():
    ok, broken, gone = _outbox(), _outbox(ref="r-broken"), _outbox(ref="r-gone")
    process = dict(scope_type="process", scope_id="pr1", memory_type="episodic")
    rows = {ROW: item(**process), "r-broken": item(**process), "r-gone": None}
    upserts, deletes = [], []

    def upsert(it):
        if upserts:
            raise RuntimeError("embedder down")
        upserts.append(it.id)

    with patch.object(vector_sync, "_upsert_memory_point", side_effect=upsert), \
         patch.object(vector_sync, "delete_memory_points", side_effect=lambda ids: deletes.append(list(ids))):
        stats = await _drain([ok, broken, gone], lambda model, key: rows[key])
    assert stats == {"done": 2, "failed": 1}
    assert ok.done_at and gone.done_at and broken.done_at is None
    assert broken.attempts == 1 and "embedder down" in broken.last_error and broken.next_attempt_at > datetime.utcnow()
    assert deletes == [["r-gone", None]]


async def test_a_user_row_left_in_the_legacy_outbox_only_leaves_the_legacy_index():
    row = _outbox()
    upserts, deletes = [], []
    with patch.object(vector_sync, "_upsert_memory_point", side_effect=lambda it: upserts.append(it.id)), \
         patch.object(vector_sync, "delete_memory_points", side_effect=lambda ids: deletes.append(list(ids))):
        stats = await _drain([row], lambda model, key: item())
    assert stats == {"done": 1, "failed": 0} and upserts == [] and deletes == [[ROW, None]]


async def test_deep_rows_are_indexed_in_one_batch_and_a_failure_retries_them_all():
    chunk, section, gone = _outbox("deep", "chunk:c1"), _outbox("deep", "section:s1"), _outbox("deep", "fact:f-gone")
    points = {
        "chunk:c1": index.IndexPoint(id="c1", level="chunk", text="Kobe beef at Mouriya", payload={}),
        "section:s1": index.IndexPoint(id="s1", level="section", text="Lunch\nKobe beef", payload={}),
        "fact:f-gone": None,
    }
    upserts, deletes = [], []
    with patch.object(index, "build_points", AsyncMock(return_value=points)), \
         patch.object(index, "upsert_points", side_effect=lambda pts: upserts.append([p.id for p in pts])), \
         patch.object(index, "delete_points", side_effect=lambda ids: deletes.append(list(ids))):
        stats = await _drain([chunk, section, gone], lambda model, key: None)
    assert stats == {"done": 3, "failed": 0} and upserts == [["c1", "s1"]] and deletes == [["f-gone"]]

    retry = [_outbox("deep", "chunk:c1"), _outbox("deep", "section:s1")]
    with patch.object(index, "build_points", AsyncMock(return_value={k: points[k] for k in ("chunk:c1", "section:s1")})), \
         patch.object(index, "upsert_points", side_effect=RuntimeError("qdrant down")), \
         patch.object(index, "delete_points", side_effect=lambda ids: None):
        stats = await _drain(retry, lambda model, key: None)
    assert stats == {"done": 0, "failed": 2}
    assert all(r.done_at is None and r.attempts == 1 and "qdrant down" in r.last_error for r in retry)


async def test_deep_rows_wait_while_deep_memory_is_off():
    statements = []
    await _drain([], lambda model, key: None, deep_enabled=False, statements=statements)
    await _drain([], lambda model, key: None, deep_enabled=True, statements=statements)
    (off_sql, off_params), (on_sql, on_params) = statements
    assert "vector_outbox.kind !=" in off_sql and "deep" in off_params.values()
    assert "vector_outbox.kind !=" not in on_sql and "deep" not in on_params.values()


async def test_reconcile_backfills_missing_rows_and_tasks_deletes_orphans_and_respects_legacy_links():
    legacy_ref = "22222222-2222-4222-8222-222222222222"
    rows = [(ROW, {}), ("r-legacy", {"vector_ref": legacy_ref}), ("r-seeded", {}), ("r-missing", {})]
    points = [(ROW, {"memory_id": ROW}), (legacy_ref, {}), ("p-seeded", {"memory_id": "r-seeded"}), ("p-orphan", {})]
    ended = [
        SimpleNamespace(id="t-indexed", goal="Compare visa rules", task_type="user", supplementary_context=None),
        SimpleNamespace(id="t-ended", goal="summarize inbox", task_type="cron", supplementary_context={}),
        SimpleNamespace(id="c1", goal="RMP CANARY: Reply with exactly CANARY_OK on its own line.", task_type="canary",
                        supplementary_context=None),
        SimpleNamespace(id="t-queued", goal="Draft the memo", task_type="user", supplementary_context=None),
        SimpleNamespace(id="t-placeholder", goal="Also give it in EUR.", task_type="user",
                        supplementary_context={"intake_reserved": False, "closed_reason": "intake_placeholder"}),
    ]
    db = MagicMock()
    results = []
    for value in (rows, [("t-indexed",), ("t-missing",)], ended, [("registry", "t-queued")]):
        r = MagicMock()
        r.all.return_value = value
        results.append(r)
    db.execute = AsyncMock(side_effect=results)
    added, deleted = [], []
    db.add_all = added.extend
    db.commit = AsyncMock()
    with patch.object(vector_sync, "AsyncSessionLocal", _session(db)), \
         patch.object(index, "is_enabled", return_value=False), \
         patch.object(vector_sync, "_scroll", side_effect=[points, [("t-indexed", None), ("t-orphan", None)]]), \
         patch.object(vector_sync, "delete_memory_points", side_effect=lambda ids: deleted.append(("memory", list(ids)))), \
         patch.object(vector_sync, "_delete_points", side_effect=lambda coll, ids: deleted.append(("registry", list(ids)))):
        stats = await vector_sync.reconcile(apply=True)
    assert stats["memory_missing"] == 1 and stats["memory_orphans"] == 1 and stats["registry_unindexed"] == 1
    assert [(o.kind, o.ref_id) for o in added] == [
        ("memory", "r-missing"), ("registry", "t-missing"), ("registry", "t-ended"),
    ]
    assert deleted == [("memory", ["p-orphan"]), ("registry", ["t-orphan"])]


def test_reconcile_refuses_to_delete_most_of_an_index():
    assert vector_sync._guarded([str(i) for i in range(80)], 100, "c") == []
    assert vector_sync._guarded(["a"], 100, "c") == ["a"]


async def test_the_deep_reconcile_queues_missing_objects_and_deletes_orphans():
    expected = {"c1": "chunk:c1", "c2": "chunk:c2", "f1": "fact:f1", "d1": "document:d1"}
    db = MagicMock()
    added, deleted = [], []
    db.add_all = added.extend
    db.commit = AsyncMock()
    with patch.object(vector_sync, "AsyncSessionLocal", _session(db)), \
         patch.object(vector_sync, "_deep_expected", AsyncMock(return_value=expected)), \
         patch.object(index, "scroll_point_ids", return_value=["c1", "orphan-1"]), \
         patch.object(index, "collection_name", return_value="rmp_deep_memory_v1"), \
         patch.object(index, "delete_points", side_effect=lambda ids: deleted.append(list(ids))):
        stats = await vector_sync._reconcile_deep(True, {("deep", "fact:f1")})
    assert stats == {"deep_objects": 4, "deep_points": 2, "deep_missing": 2, "deep_orphans": 1}
    assert sorted(o.ref_id for o in added) == ["chunk:c2", "document:d1"] and deleted == [["orphan-1"]]


async def test_recall_falls_back_to_postgres_full_text_only_when_the_index_does_not_answer():
    db = MagicMock()
    empty = MagicMock()
    empty.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=empty)
    fts = AsyncMock(return_value=[{"content": "metric units", "source": "postgres_fts"}])
    with patch.object(router, "AsyncSessionLocal", _session(db)), \
         patch.object(router, "is_vector_memory_enabled", return_value=True), \
         patch.object(router, "get_vector_service", return_value=MagicMock()), \
         patch.object(router, "_fts_search", fts):
        # User memory: the deep index answers, else Postgres text search.
        with patch.object(router, "_deep_fact_search", AsyncMock(return_value=None)):
            down = await router.MemoryRouter.read("user", "default", query="units")
        with patch.object(router, "_deep_fact_search", AsyncMock(return_value=[])):
            up = await router.MemoryRouter.read("user", "default", query="units")
        # Process memory: the legacy index.
        with patch.object(router, "_vector_search_bounded", AsyncMock(return_value=None)) as legacy:
            process_down = await router.MemoryRouter.read("process", "pr1", query="units")
    assert [h["source"] for h in down] == ["postgres_fts"] and [h["source"] for h in process_down] == ["postgres_fts"]
    assert up == [] and fts.await_count == 2 and legacy.await_count == 1


async def test_user_memory_search_asks_the_deep_index_for_this_users_facts():
    hits = [
        index.Hit(id="f1", score=0.5, payload={"ref_id": "f1", "memory_type": "semantic", "text": "Code word PELICAN-47.",
                                              "confidence": 90}),
        index.Hit(id="f2", score=0.3, payload={"ref_id": "f2", "memory_type": "semantic"}),
    ]
    calls = []

    def search(query, **kwargs):
        calls.append((query, kwargs))
        return hits

    with patch.object(index, "is_enabled", return_value=True), patch.object(index, "search", side_effect=search), \
         patch.object(index, "collection_exists", return_value=True):
        found = await router._deep_fact_search("default", "code word", 5, "semantic")
    with patch.object(index, "is_enabled", return_value=True), patch.object(index, "search", side_effect=search), \
         patch.object(index, "collection_exists", return_value=False):
        assert await router._deep_fact_search("default", "code word", 5, None) is None, "no collection yet"
    assert calls == [("code word", {"levels": ("fact",), "match": {"scope_id": "default", "memory_type": "semantic"},
                                     "limit": 5})]
    assert found == [{"id": "f1", "memory_type": "semantic", "content": "Code word PELICAN-47.", "confidence": 90,
                      "score": 0.5, "source": "vector"}]
    with patch.object(index, "is_enabled", return_value=False):
        assert await router._deep_fact_search("default", "code word", 5, None) is None


async def test_a_superseded_memory_is_never_read(tmp_path):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base, MemoryItem

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as db:
        db.add(MemoryItem(id="old", scope_type="user", scope_id="default", memory_type="semantic",
                          content="Test code word is PELICAN-47.", valid_to=datetime(2026, 9, 30)))
        db.add(MemoryItem(id="new", scope_type="user", scope_id="default", memory_type="semantic",
                          content="Test code word is HERON-12.", supersedes_memory_id="old"))
        await db.commit()
    with patch.object(router, "AsyncSessionLocal", sessions):
        read = await router.MemoryRouter.read("user", "default", "semantic")
    await engine.dispose()
    assert [m["id"] for m in read] == ["new"]


def test_a_failed_embedder_probe_is_retried_after_a_minute(monkeypatch):
    from app.memory import vector

    svc = vector.VectorMemoryService({"enabled": True})
    probes = []

    class Mem0:
        @staticmethod
        def from_config(config):
            return object()

    monkeypatch.setitem(__import__("sys").modules, "mem0", SimpleNamespace(Memory=Mem0))
    monkeypatch.setattr(svc, "_build_mem0_config", lambda: {})
    monkeypatch.setattr(svc, "_probe_embed", lambda: probes.append(1) or False)
    now = [1000.0]
    monkeypatch.setattr(vector.time, "monotonic", lambda: now[0])
    assert svc._ensure_client() is False and probes == [1]
    now[0] += vector.PROBE_RETRY_SEC - 1
    assert svc._ensure_client() is False and probes == [1]
    now[0] += 2
    monkeypatch.setattr(svc, "_probe_embed", lambda: probes.append(2) or True)
    assert svc._ensure_client() is True and probes == [1, 2]


def test_procedural_recall_is_narrowed_to_its_process_type():
    from app.memory.vector import VectorMemoryService

    svc = VectorMemoryService.__new__(VectorMemoryService)
    svc.config = {"enabled": True}
    svc._ensure_client = lambda: True
    svc._memory = MagicMock()
    svc._memory.search.return_value = {"results": []}
    assert svc.search("procedural", "user", "how to book trains") == []
    kwargs = svc._memory.search.call_args.kwargs
    assert kwargs["filters"] == {"procedural_scope_id": "user"} and kwargs["agent_id"] == "procedural"
    svc._memory.search.side_effect = RuntimeError("qdrant down")
    assert svc.search("user", "default", "units") is None


async def test_registry_indexing_through_the_outbox_retries_a_failed_vector_write():
    from app.task_registry import indexer

    summary = {"terminal_status": "completed", "intent_snippet": "Book the train", "process_type": "user"}
    with patch("app.task_registry.summary.build_task_summary", AsyncMock(return_value=summary)), \
         patch.object(indexer, "upsert_task_vector", return_value=None), \
         patch.object(indexer, "is_vector_memory_enabled", return_value=True), \
         patch.object(indexer, "upsert_registry_entry", AsyncMock(return_value="e1")) as entry:
        with pytest.raises(RuntimeError):
            await indexer.index_terminal_task("t1", require_vector=True)
        # The Postgres entry is written even though the vector write failed and will be retried.
        entry.assert_awaited_once_with("t1")
        assert await indexer.index_terminal_task("t1") == "e1"
    with patch("app.task_registry.summary.build_task_summary", AsyncMock(return_value=summary)), \
         patch.object(indexer, "upsert_task_vector", return_value="p1"), \
         patch.object(indexer, "upsert_registry_entry", AsyncMock(return_value="e1")) as entry:
        assert await indexer.index_terminal_task("t1", require_vector=True) == "e1"
        assert [c.kwargs for c in entry.await_args_list] == [{}, {"vector_point_id": "p1"}]
