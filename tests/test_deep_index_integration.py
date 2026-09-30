"""The hybrid index against the real local Qdrant, on a throwaway collection (RMP_QDRANT_IT=1)."""
import hashlib
import os
import random
import uuid

import pytest

from app.deep_memory import index

pytestmark = pytest.mark.skipif(os.environ.get("RMP_QDRANT_IT") != "1", reason="set RMP_QDRANT_IT=1 to run")


def fake_embed(texts):
    """Deterministic unit vectors, so only BM25 carries meaning here."""
    out = []
    for text in texts:
        rng = random.Random(hashlib.sha256(text.encode()).digest())
        vec = [rng.uniform(-1, 1) for _ in range(index.embed_dims())]
        norm = sum(v * v for v in vec) ** 0.5
        out.append([v / norm for v in vec])
    return out


@pytest.fixture
def collection(monkeypatch):
    name = f"rmp_it_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(index, "collection_name", lambda: name)
    monkeypatch.setattr(index, "embed_texts", fake_embed)
    index.reset_for_tests()
    yield name
    client = index._client()
    if client.collection_exists(name):
        client.delete_collection(name)
    index.reset_for_tests()


def point(obj_id, level, text, **payload):
    return index.IndexPoint(obj_id, level, text, {"valid": True, "text": text, **payload})


def test_hybrid_search_filters_and_deletes_on_real_qdrant(collection):
    code, ingress, stale, fact = (str(uuid.uuid4()) for _ in range(4))
    index.upsert_points([
        point(code, "chunk", "Kirill: my test code word is PELICAN-47.", task_id="t1", session_key="s1",
              source_at="2026-09-30T05:08:00Z"),
        point(ingress, "chunk", "The guide sets up an ingress controller with cert-manager.", task_id="t2"),
        index.IndexPoint(stale, "chunk", "Old code word PELICAN-12.", {"valid": False, "text": "old"}),
        point(fact, "fact", "Kirill's test code word is PELICAN-47.", scope_id="default", memory_type="semantic"),
    ])
    hits = index.search("PELICAN-47 code word", levels=("chunk", "fact"), weights=(1.0, 3.0))
    assert hits[0].id in (code, fact) and stale not in [h.id for h in hits]
    chunks = index.search("PELICAN-47 code word", levels=("chunk",))
    assert chunks[0].id == code and fact not in [h.id for h in chunks]
    assert [h.id for h in index.search("ingress controller", levels=("chunk",), match={"task_id": "t2"})] == [ingress]
    assert index.search("code word", levels=("chunk",), valid_only=False, match={"text": "old"})[0].id == stale
    assert sorted(index.scroll_point_ids()) == sorted([code, ingress, stale, fact])
    assert index.delete_points([code, stale]) == 2
    assert sorted(index.scroll_point_ids()) == sorted([ingress, fact])
