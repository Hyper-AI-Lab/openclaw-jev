"""Qdrant vector index for task registry summaries."""
from __future__ import annotations

import logging
import socket
import threading
from typing import Any, Dict, List, Optional

from app.config import get_task_registry_config, get_vector_memory_config
from app.memory.nvidia_embed import NvidiaEmbeddings
from app.memory.vector import _read_nvidia_key

logger = logging.getLogger("rmp.task_registry.vector")

_client = None
_lock = threading.Lock()


def _qdrant_query_timeout_sec() -> int:
    return int(get_task_registry_config().get("qdrant_query_timeout_sec", 8))


def _grpc_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _get_qdrant_client():
    global _client
    with _lock:
        if _client is not None:
            return _client
        from qdrant_client import QdrantClient

        vm = get_vector_memory_config()
        mode = (vm.get("qdrant_mode") or "embedded").strip().lower()
        if mode == "server" or vm.get("qdrant_host"):
            host = vm.get("qdrant_host", "127.0.0.1")
            http_port = int(vm.get("qdrant_port", 6333))
            grpc_port = int(vm.get("qdrant_grpc_port", 6334))
            kwargs: Dict[str, Any] = {
                "host": host,
                "port": http_port,
                "check_compatibility": False,
            }
            if _grpc_port_open(host, grpc_port):
                kwargs["grpc_port"] = grpc_port
                kwargs["prefer_grpc"] = True
            _client = QdrantClient(**kwargs)
        else:
            _client = QdrantClient(path=vm.get("qdrant_path"))
        return _client


def _embed(text: str) -> List[float]:
    vm = get_vector_memory_config()
    api_key = _read_nvidia_key()
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY required for task registry embeddings")
    model = NvidiaEmbeddings(
        api_key=api_key,
        model=vm.get("embedder_model", "nvidia/nv-embed-v1"),
    )
    return model.embed_query(text)


def _normalize_query_hits(response: Any, *, min_score: float) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    points = getattr(response, "points", None) or response or []
    for hit in points:
        score = getattr(hit, "score", None)
        if score is not None and score < min_score:
            continue
        payload = getattr(hit, "payload", None) or {}
        out.append(
            {
                "task_id": payload.get("task_id"),
                "score": score,
                "terminal_status": payload.get("terminal_status"),
                "process_type": payload.get("process_type"),
                "recurrence_key": payload.get("recurrence_key"),
                "session_key": payload.get("session_key"),
                "intent_snippet": payload.get("intent_snippet"),
            }
        )
    return out


def _ensure_collection() -> None:
    from qdrant_client.http import models as rest

    cfg = get_task_registry_config()
    name = cfg.get("collection_name", "rmp_task_registry")
    dims = int(get_vector_memory_config().get("embedding_dims", 4096))
    client = _get_qdrant_client()
    if client.collection_exists(name):
        return
    client.create_collection(
        collection_name=name,
        vectors_config=rest.VectorParams(size=dims, distance=rest.Distance.COSINE),
    )
    logger.info("Created Qdrant collection %s", name)


def registry_document_text(summary: Dict[str, Any]) -> str:
    parts = [
        f"intent: {summary.get('intent_snippet', '')}",
        f"outcome: {summary.get('outcome_summary', '')}",
        f"process_type: {summary.get('process_type', '')}",
        f"status: {summary.get('terminal_status', '')}",
        f"kind: {summary.get('task_kind', '')}",
        f"recurrence: {summary.get('recurrence_key', '')}",
    ]
    return "\n".join(p for p in parts if p.split(": ", 1)[-1])


def upsert_task_vector(task_id: str, summary: Dict[str, Any]) -> Optional[str]:
    cfg = get_task_registry_config()
    if not cfg.get("enabled", True):
        return None
    try:
        from qdrant_client.http import models as rest

        _ensure_collection()
        collection = cfg.get("collection_name", "rmp_task_registry")
        text = registry_document_text(summary)
        vector = _embed(text)
        point_id = task_id
        client = _get_qdrant_client()
        client.upsert(
            collection_name=collection,
            points=[
                rest.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "task_id": task_id,
                        "terminal_status": summary.get("terminal_status"),
                        "process_type": summary.get("process_type"),
                        "task_kind": summary.get("task_kind"),
                        "recurrence_key": summary.get("recurrence_key"),
                        "session_key": summary.get("session_key"),
                        "intent_snippet": summary.get("intent_snippet"),
                    },
                )
            ],
            wait=True,
            timeout=_qdrant_query_timeout_sec(),
        )
        return point_id
    except Exception as exc:
        logger.warning("Task registry vector upsert failed for %s: %s", task_id, exc)
        return None


def search_similar_tasks(
    query: str,
    *,
    limit: int = 5,
    min_score: float = 0.0,
) -> List[Dict[str, Any]]:
    cfg = get_task_registry_config()
    if not cfg.get("enabled", True):
        return []
    try:
        _ensure_collection()
        collection = cfg.get("collection_name", "rmp_task_registry")
        vector = _embed(query)
        client = _get_qdrant_client()
        response = client.query_points(
            collection_name=collection,
            query=vector,
            limit=limit,
            with_payload=True,
            timeout=_qdrant_query_timeout_sec(),
        )
        return _normalize_query_hits(response, min_score=min_score)
    except Exception as exc:
        logger.warning("Task registry vector search failed: %s", exc)
        return []


def probe_task_registry_vector(*, limit: int = 1) -> Dict[str, Any]:
    """Lightweight live probe for readiness checks."""
    cfg = get_task_registry_config()
    collection = cfg.get("collection_name", "rmp_task_registry")
    dims = int(get_vector_memory_config().get("embedding_dims", 4096))
    client = _get_qdrant_client()
    if not client.collection_exists(collection):
        return {"ok": True, "message": f"collection {collection} not yet created", "latency_ms": 0}
    zero = [0.0] * dims
    import time

    started = time.monotonic()
    client.query_points(
        collection_name=collection,
        query=zero,
        limit=limit,
        with_payload=False,
        timeout=_qdrant_query_timeout_sec(),
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    return {"ok": True, "message": "query_points ok", "latency_ms": latency_ms}
