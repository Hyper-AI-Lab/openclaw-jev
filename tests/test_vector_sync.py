"""Postgres and Qdrant back each other up: outbox, drain, reconcile, full-text fallback."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
        mem_id = await router.MemoryRouter.write("user", "default", "semantic", "Kirill prefers metric units.")
        await router.MemoryRouter.write("process", "pr1", "working", "scratch note for this step")
    kinds = [type(o).__name__ for o in added]
    assert kinds == ["MemoryItem", "VectorOutbox", "MemoryItem"]
    assert added[1].kind == "memory" and added[1].ref_id == mem_id
    db.commit.assert_awaited()


def _outbox(kind="memory", ref=ROW):
    return SimpleNamespace(kind=kind, ref_id=ref, attempts=0, last_error=None,
                           next_attempt_at=datetime(2026, 9, 29), done_at=None)


async def _drain(rows, db_get):
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(side_effect=db_get)
    db.commit = AsyncMock()
    with patch.object(vector_sync, "AsyncSessionLocal", _session(db)):
        return await vector_sync.drain_once()


async def test_the_drain_indexes_retries_with_backoff_and_deletes_gone_rows():
    ok, broken, gone = _outbox(), _outbox(ref="r-broken"), _outbox(ref="r-gone")
    rows = {ROW: item(), "r-broken": item(), "r-gone": None}
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
        with patch.object(router, "_vector_search_bounded", AsyncMock(return_value=None)):
            down = await router.MemoryRouter.read("user", "default", query="units")
        with patch.object(router, "_vector_search_bounded", AsyncMock(return_value=[])):
            up = await router.MemoryRouter.read("user", "default", query="units")
    assert [h["source"] for h in down] == ["postgres_fts"]
    assert up == [] and fts.await_count == 1


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
        entry.assert_not_awaited()
        assert await indexer.index_terminal_task("t1") == "e1"
