"""The deep-memory hybrid index adapter, against a recording fake Qdrant and a real SQLite schema."""
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from qdrant_client import models
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, DeepChunk, DeepDocument, DeepSection, MemoryItem
from app.deep_memory import index


class FakeQdrant:
    def __init__(self, exists=False, dims=1536, sparse=True):
        self.exists, self.dims, self.sparse = exists, dims, sparse
        self.calls = []
        self.points = []

    def collection_exists(self, name):
        return self.exists

    def create_collection(self, name, vectors_config, sparse_vectors_config):
        self.calls.append(("create_collection", name, vectors_config, sparse_vectors_config))
        self.exists = True

    def get_collection(self, name):
        vectors = {index.DENSE: models.VectorParams(size=self.dims, distance=models.Distance.COSINE)}
        sparse = {index.SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)} if self.sparse else {}
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=vectors, sparse_vectors=sparse)))

    def create_payload_index(self, name, field_name, field_schema, wait=True):
        self.calls.append(("index", field_name, field_schema))

    def upsert(self, name, points, wait=True):
        self.calls.append(("upsert", name, points))

    def delete(self, name, points_selector, wait=True):
        self.calls.append(("delete", list(points_selector.points)))

    def scroll(self, name, limit, offset, with_payload, with_vectors):
        start = offset or 0
        page = self.points[start:start + limit]
        nxt = start + limit if start + limit < len(self.points) else None
        return [SimpleNamespace(id=p) for p in page], nxt

    def query_points(self, name, **kwargs):
        self.calls.append(("query", kwargs))
        return SimpleNamespace(points=[SimpleNamespace(id="c1", score=0.8, payload={"text": "hit"})])


@pytest.fixture
def fake(monkeypatch):
    index.reset_for_tests()
    client = FakeQdrant()
    monkeypatch.setattr(index, "_client", lambda: client)
    monkeypatch.setattr(index, "embed_texts", lambda texts: [[0.1] * 4 for _ in texts])
    yield client
    index.reset_for_tests()


def test_refs_round_trip_and_bad_refs_are_rejected():
    assert index.parse_ref(index.point_ref("chunk", "c1")) == ("chunk", "c1")
    for bad in ("", "chunk:", "page:p1", "c1"):
        with pytest.raises(ValueError):
            index.parse_ref(bad)
    assert index.iso_utc(datetime(2026, 9, 30, 2, 48, 19, 123)) == "2026-09-30T02:48:19Z"
    assert index.iso_utc(datetime(2026, 9, 30, 11, 48, tzinfo=timezone.utc)) == "2026-09-30T11:48:00Z"


def test_the_collection_gets_both_vectors_and_every_payload_index_once(fake):
    index.ensure_collection()
    index.ensure_collection()
    created = [c for c in fake.calls if c[0] == "create_collection"]
    assert len(created) == 1
    _, name, vectors, sparse = created[0]
    assert name == "rmp_deep_memory_v1" and vectors[index.DENSE].size == 1536
    assert sparse[index.SPARSE].modifier == models.Modifier.IDF
    fields = [c[1] for c in fake.calls if c[0] == "index"]
    assert fields == [f for f, _ in index.PAYLOAD_INDEXES]
    assert fake.calls.index(created[0]) < fake.calls.index(("index", "level", models.PayloadSchemaType.KEYWORD))


def test_an_existing_collection_with_other_vectors_is_refused(monkeypatch):
    index.reset_for_tests()
    for client in (FakeQdrant(exists=True, dims=3072), FakeQdrant(exists=True, sparse=False)):
        monkeypatch.setattr(index, "_client", lambda client=client: client)
        with pytest.raises(RuntimeError):
            index.ensure_collection()
    index.reset_for_tests()


def test_upserted_points_carry_dense_and_server_side_bm25_vectors(fake):
    index.upsert_points([index.IndexPoint("c1", "chunk", "Kirill's code word is\nPELICAN-47.", {"task_id": "t1"})])
    [(_, name, points)] = [c for c in fake.calls if c[0] == "upsert"]
    point = points[0]
    assert point.id == "c1" and point.vector[index.DENSE] == [0.1] * 4
    bm25 = point.vector[index.SPARSE]
    assert (bm25.model, bm25.options, bm25.text) == ("qdrant/bm25", {"language": "english"},
                                                     "Kirill's code word is PELICAN-47.")
    assert point.payload == {"task_id": "t1", "level": "chunk", "ref_id": "c1"}


def test_search_filters_both_prefetches_and_fuses_by_weighted_rank(fake, monkeypatch):
    fake.exists = True
    monkeypatch.setattr(index, "embed_query", lambda text: [0.2] * 4)
    since = datetime(2026, 9, 1)
    hits = index.search(
        "code word", levels=("chunk", "fact"), match={"session_key": "s1"}, match_any={"task_id": ["t1", "t2"]},
        since=since, limit=5, weights=(1.0, 2.0),
    )
    assert hits == [index.Hit("c1", 0.8, {"text": "hit"})]
    [(_, kwargs)] = [c for c in fake.calls if c[0] == "query"]
    dense, sparse = kwargs["prefetch"]
    assert dense.using == "dense" and dense.query == [0.2] * 4 and sparse.using == "bm25"
    assert sparse.query.text == "code word" and dense.filter == sparse.filter
    must = {c.key: c for c in dense.filter.must}
    assert must["level"].match.any == ["chunk", "fact"] and must["valid"].match.value is True
    assert must["session_key"].match.value == "s1" and must["task_id"].match.any == ["t1", "t2"]
    assert must["source_at"].range.gte == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert must["source_at"].range.lte is None
    assert kwargs["query"].rrf.k == index.RRF_K and kwargs["query"].rrf.weights == [1.0, 2.0]
    assert kwargs["limit"] == 5 and kwargs["with_payload"] is True


def test_a_dense_floor_ranks_only_points_close_in_meaning(fake, monkeypatch):
    fake.exists = True
    monkeypatch.setattr(index, "embed_query", lambda text: [0.2] * 4)
    replies = [SimpleNamespace(points=[SimpleNamespace(id="c1", score=0.62, payload={})]),
               SimpleNamespace(points=[SimpleNamespace(id="c1", score=0.5, payload={"text": "hit"})])]
    fake.query_points = lambda name, **kw: (fake.calls.append(("query", kw)), replies.pop(0))[1]
    assert [h.id for h in index.search("code word", levels=("fact",), dense_floor=0.3)] == ["c1"]
    (_, close), (_, fused) = [c for c in fake.calls if c[0] == "query"]
    assert close["score_threshold"] == 0.3 and close["using"] == "dense"
    has_id = [c for c in fused["prefetch"][1].filter.must if isinstance(c, models.HasIdCondition)]
    assert has_id and has_id[0].has_id == ["c1"]
    fake.calls.clear()
    fake.query_points = lambda name, **kw: (fake.calls.append(("query", kw)), SimpleNamespace(points=[]))[1]
    assert index.search("unrelated", levels=("fact",), dense_floor=0.3) == []
    assert len(fake.calls) == 1, "nothing close enough: no fused query at all"


def test_a_missing_collection_answers_empty_not_error(fake):
    assert index.search("anything", levels=("chunk",)) == []
    assert index.delete_points(["a", "b"]) == 0 and index.scroll_point_ids() == []


def test_deletes_are_batched_deduplicated_and_scroll_pages_through(fake):
    fake.exists = True
    assert index.delete_points(["a", "b", "a", None, ""]) == 2
    assert [c for c in fake.calls if c[0] == "delete"] == [("delete", ["a", "b"])]
    fake.points = [f"p{i}" for i in range(2500)]
    assert index.scroll_point_ids() == fake.points


def test_embed_texts_batches_in_order_with_dimensions_and_records_usage(monkeypatch):
    requests, recorded = [], []

    class Embeddings:
        def create(self, model, input, dimensions):
            requests.append((model, list(input), dimensions))
            data = [SimpleNamespace(index=i, embedding=[float(len(t))]) for i, t in enumerate(input)]
            return SimpleNamespace(data=list(reversed(data)), usage=SimpleNamespace(prompt_tokens=7, total_tokens=7))

    monkeypatch.setattr(index, "_read_openai_key", lambda: "sk-test")
    monkeypatch.setattr(index, "_openai_client", lambda key: SimpleNamespace(embeddings=Embeddings()))
    monkeypatch.setattr("app.llm.usage_monitor.record_request", lambda *a, **k: recorded.append((a, k)))
    monkeypatch.setattr(index, "EMBED_BATCH", 2)
    vectors = index.embed_texts(["a", "bb", "ccc"])
    assert vectors == [[1.0], [2.0], [3.0]]
    assert requests == [("text-embedding-3-large", ["a", "bb"], 1536), ("text-embedding-3-large", ["ccc"], 1536)]
    assert len(recorded) == 2 and recorded[0][0] == ("openai:default", "embed")
    with pytest.raises(ValueError):
        index.embed_texts(["ok", "   "])
    monkeypatch.setattr(index, "_read_openai_key", lambda: "")
    with pytest.raises(RuntimeError):
        index.embed_texts(["a"])


def test_the_embedding_client_is_reused_per_key(monkeypatch):
    monkeypatch.setattr(index, "_openai_clients", {})
    first = index._openai_client("sk-one")
    assert index._openai_client("sk-one") is first and index._openai_client("sk-two") is not first


def test_a_query_is_embedded_once_for_concurrent_searches_and_again_after_the_ttl(monkeypatch):
    index.reset_for_tests()
    calls = []

    def slow_embed(texts):
        calls.append(list(texts))
        time.sleep(0.1)
        return [[0.5]]

    monkeypatch.setattr(index, "embed_texts", slow_embed)
    results = []
    threads = [threading.Thread(target=lambda: results.append(index.embed_query("code  word"))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [[0.5], [0.5]] and len(calls) == 1
    assert index.embed_query("code word") == [0.5] and len(calls) == 1
    monkeypatch.setattr(index, "QUERY_CACHE_TTL_SEC", 0.0)
    time.sleep(0.01)
    index.embed_query("code word")
    assert len(calls) == 2
    index.reset_for_tests()


@pytest.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dm.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        yield session
    await engine.dispose()


async def test_points_are_built_from_the_record_and_invalid_rows_become_deletions(db):
    said = datetime(2026, 9, 30, 5, 8)
    db.add_all([
        DeepDocument(id="d1", kind="task", source_key="task:t1", title="Kobe day plan", task_id=None,
                     session_key="s1", summary="A day in Kobe with beef at lunch.", source_at=said,
                     toc=[{"section_id": "s1", "path": "Conversation", "title": "Conversation", "ordinal": 0}]),
        DeepDocument(id="d-old", kind="deliverable", source_key="deliverable:m0", valid_to=said),
        DeepSection(id="s1", document_id="d1", ordinal=0, path="Conversation", title="Conversation",
                    summary="Kirill asked for a Kobe plan."),
        DeepSection(id="s-empty", document_id="d1", ordinal=1, path="Actions", title="Actions"),
        DeepChunk(id="c1", document_id="d1", section_id="s1", ordinal=0, text="Kirill: Plan a day in Kobe.",
                  context_header="Kirill's request that opens the Kobe task.", session_key="s1", role="user",
                  message_id="m1", source_at=said, meta={"kind": "request"}),
        DeepChunk(id="c-stale", document_id="d1", ordinal=1, text="old text", valid_to=said),
        DeepChunk(id="c-orphan", document_id="d-old", ordinal=0, text="from a retired document"),
        MemoryItem(id="f1", scope_type="user", scope_id="default", memory_type="semantic",
                   content="Kirill's test code word is PELICAN-47.", confidence=90, valid_from=said),
        MemoryItem(id="f-proc", scope_type="procedural", scope_id="user", memory_type="procedural", content="x"),
        MemoryItem(id="f-old", scope_type="user", scope_id="default", memory_type="semantic", content="y",
                   valid_to=said),
    ])
    await db.commit()
    refs = ["chunk:c1", "chunk:c-stale", "chunk:c-orphan", "chunk:nope", "section:s1", "section:s-empty",
            "document:d1", "document:d-old", "fact:f1", "fact:f-proc", "fact:f-old", "junk"]
    points = await index.build_points(db, refs)
    assert set(points) == set(refs)
    assert {r for r, p in points.items() if p is not None} == {"chunk:c1", "section:s1", "document:d1", "fact:f1"}
    chunk = points["chunk:c1"]
    assert chunk.text == "Kirill's request that opens the Kobe task.\n\nKirill: Plan a day in Kobe."
    assert chunk.payload == {
        "ref_id": "c1", "document_id": "d1", "section_id": "s1", "section_path": "Conversation",
        "title": "Kobe day plan", "session_key": "s1", "message_id": "m1", "role": "user", "source_kind": "task",
        "source_at": "2026-09-30T05:08:00Z", "valid": True, "text": "Kirill: Plan a day in Kobe.",
        "header": "Kirill's request that opens the Kobe task.", "meta": {"kind": "request"},
    }
    assert points["section:s1"].text == "Conversation\nKirill asked for a Kobe plan."
    doc = points["document:d1"]
    assert doc.text == "Kobe day plan\nA day in Kobe with beef at lunch."
    assert doc.payload["toc"] == [{"section_id": "s1", "path": "Conversation", "title": "Conversation"}]
    fact = points["fact:f1"]
    assert fact.text == "Kirill's test code word is PELICAN-47."
    assert (fact.payload["scope_id"], fact.payload["memory_type"], fact.payload["confidence"]) == ("default", "semantic", 90)


async def test_text_search_is_postgres_only(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fts.db'}")
    monkeypatch.setattr(index, "AsyncSessionLocal", sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))
    assert await index.fts_search("code word", levels=("chunk", "fact")) is None
    await engine.dispose()
