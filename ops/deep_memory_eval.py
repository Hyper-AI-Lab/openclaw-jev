"""Retrieval eval for deep memory: hybrid (dense + BM25) against dense-only, recall@k on a labelled set.

Offline (default) it validates the fixture. With --live it embeds the corpus into a throwaway
Qdrant collection, runs every query four ways (hybrid RRF as the index searches, hybrid DBSF,
dense only, BM25 only), prints recall@k and MRR, and deletes the collection. The live
collection is never touched.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Sequence

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/deep_memory_eval.json"
NAMESPACE = uuid.UUID("5d1b2c1e-8f4b-4a57-9d4e-2f0f5a6c7e91")
MODES = ("hybrid_rrf", "hybrid_dbsf", "dense", "bm25")
# The plan's acceptance targets.
GATE = {"min_hybrid_recall": 0.9, "hybrid_not_below_dense": True}


def load(path: Path = FIXTURE) -> dict:
    from app.deep_memory.index import LEVELS

    data = json.loads(path.read_text())
    ids = [c["id"] for c in data["corpus"]]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError("corpus ids must be unique and present")
    if any(c["level"] not in LEVELS or not c["text"].strip() for c in data["corpus"]):
        raise ValueError("every corpus item needs a known level and text")
    queries = data["queries"]
    if len({q["id"] for q in queries}) != len(queries) or not queries:
        raise ValueError("query ids must be unique and present")
    for q in queries:
        if not q["relevant"] or set(q["relevant"]) - set(ids):
            raise ValueError(f"{q['id']}: relevant items must exist in the corpus")
    return data


def recall_at(ranked: Sequence[str], relevant: Sequence[str], k: int) -> float:
    return len(set(ranked[:k]) & set(relevant)) / len(set(relevant))


def reciprocal_rank(ranked: Sequence[str], relevant: Sequence[str]) -> float:
    return next((1 / n for n, item in enumerate(ranked, 1) if item in set(relevant)), 0.0)


def summarize(runs: Dict[str, Dict[str, List[str]]], queries: List[dict], k: int) -> dict:
    """Mean recall@k and MRR per mode, by query kind, and the hybrid misses."""
    out: dict = {"k": k, "queries": len(queries), "modes": {}}
    for mode, ranked in runs.items():
        recalls = {q["id"]: recall_at(ranked[q["id"]], q["relevant"], k) for q in queries}
        by_kind: Dict[str, List[float]] = {}
        for q in queries:
            by_kind.setdefault(q["kind"], []).append(recalls[q["id"]])
        out["modes"][mode] = {
            "recall": round(sum(recalls.values()) / len(queries), 3),
            "mrr": round(sum(reciprocal_rank(ranked[q["id"]], q["relevant"]) for q in queries) / len(queries), 3),
            "by_kind": {kind: round(sum(v) / len(v), 3) for kind, v in sorted(by_kind.items())},
            "misses": {qid: r for qid, r in recalls.items() if r < 1},
        }
    hybrid, dense = out["modes"]["hybrid_rrf"]["recall"], out["modes"]["dense"]["recall"]
    out["gate"] = {**GATE, "passed": hybrid >= GATE["min_hybrid_recall"] and hybrid >= dense}
    return out


def point_id(item_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, item_id))


def run_live(data: dict) -> dict:
    from qdrant_client import models

    from app.deep_memory import index

    k = int(data["k"])
    scratch = f"rmp_deep_memory_eval_{int(time.time())}"
    index.collection_name = lambda: scratch
    client = index._client()
    try:
        index.upsert_points([
            index.IndexPoint(id=point_id(c["id"]), level=c["level"], text=c["text"],
                             payload={"ref_id": c["id"], "valid": True, "text": c["text"]})
            for c in data["corpus"]
        ])
        flt = index._filter(index.LEVELS, None, None, None, None, True)
        runs: Dict[str, Dict[str, List[str]]] = {mode: {} for mode in MODES}
        for q in data["queries"]:
            vector = index.embed_query(q["query"])
            words = models.Document(text=index.bm25_text(q["query"]), model=index.BM25_MODEL,
                                    options=index.BM25_OPTIONS)
            prefetch = [models.Prefetch(query=vector, using=index.DENSE, filter=flt, limit=40),
                        models.Prefetch(query=words, using=index.SPARSE, filter=flt, limit=40)]
            ranked = {
                "hybrid_rrf": index.search(q["query"], levels=index.LEVELS, limit=k),
                "hybrid_dbsf": client.query_points(scratch, prefetch=prefetch, limit=k, with_payload=True,
                                                   query=models.FusionQuery(fusion=models.Fusion.DBSF)).points,
                "dense": client.query_points(scratch, query=vector, using=index.DENSE, query_filter=flt, limit=k,
                                             with_payload=True).points,
                "bm25": client.query_points(scratch, query=words, using=index.SPARSE, query_filter=flt, limit=k,
                                            with_payload=True).points,
            }
            for mode, hits in ranked.items():
                runs[mode][q["id"]] = [h.payload["ref_id"] for h in hits]
    finally:
        client.delete_collection(scratch)
    return {"collection": scratch, "deleted": not client.collection_exists(scratch),
            "embedder": index.embed_model(), **summarize(runs, data["queries"], k)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=FIXTURE)
    parser.add_argument("--live", action="store_true", help="Embed and query in a throwaway collection (API cost).")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data = load(args.dataset)
    result = run_live(data) if args.live else {
        "live": False, "corpus": len(data["corpus"]), "queries": len(data["queries"]),
        "message": "Dataset validated. No embedding or search was run."}
    rendered = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
