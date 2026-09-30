"""The deep-memory retrieval eval: its labelled fixture and its metrics (the live run is ops/deep_memory_eval.py)."""
import json

import pytest

from ops import deep_memory_eval as ev


def test_the_committed_fixture_validates():
    data = ev.load()
    assert len(data["corpus"]) >= 50 and len(data["queries"]) >= 30 and data["k"] == 8
    assert {q["kind"] for q in data["queries"]} >= {"exact", "paraphrase"}


def test_a_query_pointing_at_a_missing_item_is_refused(tmp_path):
    data = json.loads(ev.FIXTURE.read_text())
    data["queries"][0]["relevant"] = ["nope"]
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="relevant items must exist"):
        ev.load(broken)


def test_recall_mrr_and_the_gate():
    queries = [{"id": "a", "kind": "exact", "relevant": ["x", "y"]}, {"id": "b", "kind": "paraphrase", "relevant": ["z"]}]
    runs = {
        "hybrid_rrf": {"a": ["x", "q", "y"], "b": ["z"]},
        "hybrid_dbsf": {"a": ["x", "y"], "b": ["z"]},
        "dense": {"a": ["q", "x"], "b": ["w", "z"]},
        "bm25": {"a": ["y"], "b": []},
    }
    out = ev.summarize(runs, queries, k=2)
    assert out["modes"]["hybrid_rrf"]["recall"] == 0.75 and out["modes"]["hybrid_rrf"]["misses"] == {"a": 0.5}
    assert out["modes"]["dense"]["mrr"] == 0.5 and out["modes"]["bm25"]["by_kind"] == {"exact": 0.5, "paraphrase": 0.0}
    assert out["gate"]["passed"] is False
    runs["hybrid_rrf"]["a"] = ["y", "x"]
    assert ev.summarize(runs, queries, k=2)["gate"]["passed"] is True
