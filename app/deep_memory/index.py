"""The deep-memory hybrid index: one Qdrant collection, dense plus BM25.

Points are chunks, section and document summaries, and user facts. Postgres holds the
record (dm_* tables, memory_items); the vector outbox keeps this index in step, and
Postgres text search answers when Qdrant cannot.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_deep_memory_config, get_task_registry_config, is_vector_memory_enabled
from app.db.database import AsyncSessionLocal
from app.db.models import DeepChunk, DeepDocument, DeepSection, MemoryItem
from app.memory.policy import redact_secrets
from app.memory.vector import INDEXABLE_TYPES, _read_openai_key

logger = logging.getLogger("rmp.deep_memory.index")

LEVELS = ("chunk", "section", "document", "fact")
DENSE = "dense"
SPARSE = "bm25"
BM25_MODEL = "qdrant/bm25"
BM25_OPTIONS = {"language": "english"}
# Qdrant's RRF constant; its default, and what its hybrid-search tuning guide starts from.
RRF_K = 2
EMBED_BATCH = 64
EMBED_INPUT_CHARS = 8000
PAYLOAD_TEXT_CHARS = 2000
QUERY_CACHE_TTL_SEC = 300.0
QUERY_CACHE_MAX = 512
PAYLOAD_INDEXES = (
    ("level", "keyword"),
    ("ref_id", "keyword"),
    ("document_id", "keyword"),
    ("task_id", "keyword"),
    ("session_key", "keyword"),
    ("source_kind", "keyword"),
    ("scope_id", "keyword"),
    ("memory_type", "keyword"),
    ("valid", "bool"),
    ("source_at", "datetime"),
)
_FTS_WORD = re.compile(r"[^\W_]{3,}", re.UNICODE)


@dataclass
class IndexPoint:
    id: str
    level: str
    text: str
    payload: Dict[str, Any]


@dataclass
class Hit:
    id: str
    score: float
    payload: Dict[str, Any]


def collection_name() -> str:
    return str(get_deep_memory_config().get("collection_name") or "rmp_deep_memory_v1")


def embed_model() -> str:
    return str(get_deep_memory_config().get("embedder_model") or "text-embedding-3-large")


def embed_dims() -> int:
    return int(get_deep_memory_config().get("embedding_dims") or 1536)


def is_enabled() -> bool:
    return bool(get_deep_memory_config().get("enabled")) and is_vector_memory_enabled()


def point_ref(level: str, obj_id: str) -> str:
    return f"{level}:{obj_id}"


def parse_ref(ref: str) -> Tuple[str, str]:
    level, _, obj_id = str(ref or "").partition(":")
    if level not in LEVELS or not obj_id:
        raise ValueError(f"bad deep-memory ref {ref!r}")
    return level, obj_id


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def bm25_text(text: str) -> str:
    return " ".join(str(text or "").split())[:EMBED_INPUT_CHARS]


def _client():
    from app.task_registry.vector_store import _get_qdrant_client

    return _get_qdrant_client()


_ensured: set = set()
_ensure_lock = threading.Lock()


def collection_exists() -> bool:
    name = collection_name()
    if name in _ensured:
        return True
    return bool(_client().collection_exists(name))


def count_points() -> int:
    return int(_client().count(collection_name(), exact=True).count)


def ensure_collection() -> None:
    """Create the collection and its payload indexes once per process (indexes before points)."""
    from qdrant_client import models

    name = collection_name()
    with _ensure_lock:
        if name in _ensured:
            return
        client = _client()
        dims = embed_dims()
        if not client.collection_exists(name):
            client.create_collection(
                name,
                vectors_config={DENSE: models.VectorParams(size=dims, distance=models.Distance.COSINE)},
                sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
            )
            logger.info("Deep-memory collection %s created (%s dims + BM25)", name, dims)
        else:
            params = client.get_collection(name).config.params
            dense = (params.vectors or {}).get(DENSE) if isinstance(params.vectors, dict) else None
            if dense is None or int(dense.size) != dims or SPARSE not in (params.sparse_vectors or {}):
                raise RuntimeError(
                    f"collection {name} does not have a {dims}-dim '{DENSE}' vector and a '{SPARSE}' sparse vector"
                )
        schemas = {
            "keyword": models.PayloadSchemaType.KEYWORD,
            "bool": models.PayloadSchemaType.BOOL,
            "datetime": models.PayloadSchemaType.DATETIME,
        }
        for field, kind in PAYLOAD_INDEXES:
            client.create_payload_index(name, field_name=field, field_schema=schemas[kind], wait=True)
        _ensured.add(name)


_openai_clients: Dict[str, Any] = {}
_openai_lock = threading.Lock()


def _openai_client(key: str):
    """One client per key, so embeddings reuse its connection instead of a new TLS handshake."""
    from openai import OpenAI

    with _openai_lock:
        client = _openai_clients.get(key)
        if client is None:
            client = _openai_clients[key] = OpenAI(api_key=key, timeout=30.0, max_retries=2)
        return client


def _normalize(text: str) -> str:
    return " ".join(str(text or "").split())[:EMBED_INPUT_CHARS]


def embed_texts(texts: Sequence[str]) -> List[List[float]]:
    from app.llm.usage_monitor import record_request

    key = _read_openai_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY missing; deep-memory embedder not ready")
    inputs = [_normalize(t) for t in texts]
    if any(not t for t in inputs):
        raise ValueError("cannot embed empty text")
    client = _openai_client(key)
    model, dims = embed_model(), embed_dims()
    vectors: List[List[float]] = []
    for start in range(0, len(inputs), EMBED_BATCH):
        batch = inputs[start:start + EMBED_BATCH]
        response = client.embeddings.create(model=model, input=batch, dimensions=dims)
        vectors += [list(item.embedding) for item in sorted(response.data, key=lambda d: d.index)]
        usage = getattr(response, "usage", None)
        if usage is not None:
            record_request(
                "openai:default",
                "embed",
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
                model=model,
            )
    return vectors


_query_cache: "OrderedDict[Tuple[str, int, str], Tuple[float, List[float]]]" = OrderedDict()
_query_cache_lock = threading.Lock()
_inflight: Dict[Tuple[str, int, str], threading.Lock] = {}
_inflight_guard = threading.Lock()


def _cached_query(key: Tuple[str, int, str]) -> Optional[List[float]]:
    with _query_cache_lock:
        item = _query_cache.get(key)
        if item is None:
            return None
        if time.monotonic() - item[0] > QUERY_CACHE_TTL_SEC:
            _query_cache.pop(key, None)
            return None
        _query_cache.move_to_end(key)
        return list(item[1])


def embed_query(text: str) -> List[float]:
    """The query's vector, embedded once per few minutes however many searches ask for it."""
    key = (embed_model(), embed_dims(), _normalize(text))
    cached = _cached_query(key)
    if cached is not None:
        return cached
    with _inflight_guard:
        lock = _inflight.setdefault(key, threading.Lock())
    with lock:
        cached = _cached_query(key)
        if cached is not None:
            return cached
        vector = embed_texts([text])[0]
        with _query_cache_lock:
            _query_cache[key] = (time.monotonic(), list(vector))
            _query_cache.move_to_end(key)
            while len(_query_cache) > QUERY_CACHE_MAX:
                _query_cache.popitem(last=False)
    with _inflight_guard:
        _inflight.pop(key, None)
    return vector


def clear_query_cache() -> None:
    with _query_cache_lock:
        _query_cache.clear()
    with _inflight_guard:
        _inflight.clear()


def reset_for_tests() -> None:
    clear_query_cache()
    with _ensure_lock:
        _ensured.clear()


def upsert_points(points: Sequence[IndexPoint]) -> None:
    from qdrant_client import models

    if not points:
        return
    ensure_collection()
    client, name = _client(), collection_name()
    for start in range(0, len(points), EMBED_BATCH):
        batch = list(points[start:start + EMBED_BATCH])
        vectors = embed_texts([p.text for p in batch])
        client.upsert(
            name,
            points=[
                models.PointStruct(
                    id=p.id,
                    vector={
                        DENSE: vector,
                        SPARSE: models.Document(text=bm25_text(p.text), model=BM25_MODEL, options=BM25_OPTIONS),
                    },
                    payload={**p.payload, "level": p.level, "ref_id": p.payload.get("ref_id") or p.id},
                )
                for p, vector in zip(batch, vectors)
            ],
            wait=True,
        )


def delete_points(ids: Iterable[Optional[str]]) -> int:
    from qdrant_client import models

    unique = [i for i in dict.fromkeys(ids) if i]
    if not unique:
        return 0
    client, name = _client(), collection_name()
    if not client.collection_exists(name):
        return 0
    for start in range(0, len(unique), 256):
        client.delete(name, points_selector=models.PointIdsList(points=unique[start:start + 256]), wait=True)
    return len(unique)


def scroll_point_ids() -> List[str]:
    client, name = _client(), collection_name()
    if not client.collection_exists(name):
        return []
    out: List[str] = []
    offset = None
    while True:
        points, offset = client.scroll(name, limit=1024, offset=offset, with_payload=False, with_vectors=False)
        out += [str(p.id) for p in points]
        if offset is None:
            return out


def _filter(
    levels: Sequence[str],
    match: Optional[Dict[str, Any]],
    match_any: Optional[Dict[str, Sequence[Any]]],
    since: Optional[datetime],
    until: Optional[datetime],
    valid_only: bool,
):
    from qdrant_client import models

    must: List[Any] = [models.FieldCondition(key="level", match=models.MatchAny(any=list(levels)))]
    if valid_only:
        must.append(models.FieldCondition(key="valid", match=models.MatchValue(value=True)))
    for key, value in (match or {}).items():
        must.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))
    for key, values in (match_any or {}).items():
        must.append(models.FieldCondition(key=key, match=models.MatchAny(any=list(values))))
    if since is not None or until is not None:
        must.append(
            models.FieldCondition(key="source_at", range=models.DatetimeRange(gte=iso_utc(since), lte=iso_utc(until)))
        )
    return models.Filter(must=must)


def search(
    query: str,
    *,
    levels: Sequence[str],
    match: Optional[Dict[str, Any]] = None,
    match_any: Optional[Dict[str, Sequence[Any]]] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    valid_only: bool = True,
    limit: int = 10,
    prefetch_limit: int = 40,
    weights: Tuple[float, float] = (1.0, 1.0),
    timeout_sec: Optional[int] = None,
    dense_floor: Optional[float] = None,
) -> List[Hit]:
    """Dense and BM25 candidates under the same filter, fused by weighted reciprocal rank.

    With ``dense_floor``, only points at least that similar to the query in meaning are
    ranked: BM25 alone matches on shared words, which is not relevance.
    """
    from qdrant_client import models

    query = (query or "").strip()
    if not query:
        return []
    client, name = _client(), collection_name()
    if not client.collection_exists(name):
        return []
    flt = _filter(levels, match, match_any, since, until, valid_only)
    timeout = timeout_sec or int(get_task_registry_config().get("qdrant_query_timeout_sec", 8))
    vector = embed_query(query)
    if dense_floor is not None:
        close = client.query_points(
            name, query=vector, using=DENSE, query_filter=flt, limit=prefetch_limit,
            score_threshold=dense_floor, with_payload=False, timeout=timeout,
        ).points
        if not close:
            return []
        flt = models.Filter(must=[*flt.must, models.HasIdCondition(has_id=[p.id for p in close])])
    response = client.query_points(
        name,
        prefetch=[
            models.Prefetch(query=vector, using=DENSE, filter=flt, limit=prefetch_limit),
            models.Prefetch(
                query=models.Document(text=bm25_text(query), model=BM25_MODEL, options=BM25_OPTIONS),
                using=SPARSE,
                filter=flt,
                limit=prefetch_limit,
            ),
        ],
        query=models.RrfQuery(rrf=models.Rrf(k=RRF_K, weights=list(weights))),
        limit=limit,
        with_payload=True,
        timeout=timeout,
    )
    return [Hit(id=str(p.id), score=float(p.score), payload=dict(p.payload or {})) for p in response.points]


def _clean(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in payload.items() if v is not None}


def _document_valid(doc: Optional[DeepDocument]) -> bool:
    return doc is not None and doc.valid_to is None


def _chunk_payload(chunk: DeepChunk, doc: DeepDocument, section: Optional[DeepSection]) -> Dict[str, Any]:
    return _clean(
        {
            "ref_id": chunk.id,
            "document_id": chunk.document_id,
            "section_id": chunk.section_id,
            "section_path": (section.path if section else "") or "",
            "title": doc.title or "",
            "task_id": chunk.task_id,
            "session_key": chunk.session_key,
            "process_run_id": chunk.process_run_id,
            "message_id": chunk.message_id,
            "role": chunk.role,
            "source_kind": doc.kind,
            "source_at": iso_utc(chunk.source_at or doc.source_at or chunk.created_at),
            "valid": True,
            "text": (chunk.text or "")[:PAYLOAD_TEXT_CHARS],
            "header": (chunk.context_header or "")[:600],
            "meta": chunk.meta or {},
        }
    )


def _section_payload(section: DeepSection, doc: DeepDocument) -> Dict[str, Any]:
    return _clean(
        {
            "ref_id": section.id,
            "document_id": section.document_id,
            "section_id": section.id,
            "section_path": section.path or section.title or "",
            "title": doc.title or "",
            "task_id": doc.task_id,
            "session_key": doc.session_key,
            "source_kind": doc.kind,
            "source_at": iso_utc(doc.source_at or doc.created_at),
            "valid": True,
            "text": (section.summary or "")[:PAYLOAD_TEXT_CHARS],
        }
    )


def _document_payload(doc: DeepDocument) -> Dict[str, Any]:
    toc = [
        {"section_id": e.get("section_id"), "path": e.get("path"), "title": e.get("title")}
        for e in (doc.toc or [])[:40]
        if isinstance(e, dict)
    ]
    return _clean(
        {
            "ref_id": doc.id,
            "document_id": doc.id,
            "title": doc.title or "",
            "task_id": doc.task_id,
            "session_key": doc.session_key,
            "source_kind": doc.kind,
            "source_at": iso_utc(doc.source_at or doc.created_at),
            "valid": True,
            "text": (doc.summary or "")[:PAYLOAD_TEXT_CHARS],
            "toc": toc,
        }
    )


def _fact_payload(item: MemoryItem) -> Dict[str, Any]:
    return _clean(
        {
            "ref_id": item.id,
            "scope_type": item.scope_type,
            "scope_id": item.scope_id,
            "memory_type": item.memory_type,
            "source_kind": "memory",
            "source_at": iso_utc(item.valid_from or item.created_at),
            "valid": True,
            "text": redact_secrets(item.content or "")[:PAYLOAD_TEXT_CHARS],
            "confidence": item.confidence,
            "subject": (item.provenance_ref or {}).get("subject"),
        }
    )


async def build_points(db: AsyncSession, refs: Sequence[str]) -> Dict[str, Optional[IndexPoint]]:
    """An index point for every ref, or None when its row is gone or no longer valid."""
    wanted: Dict[str, List[str]] = {level: [] for level in LEVELS}
    out: Dict[str, Optional[IndexPoint]] = {}
    for ref in refs:
        out[ref] = None
        try:
            level, obj_id = parse_ref(ref)
        except ValueError:
            continue
        wanted[level].append(obj_id)

    async def rows(model, ids):
        if not ids:
            return {}
        return {r.id: r for r in (await db.execute(select(model).where(model.id.in_(ids)))).scalars().all()}

    chunks = await rows(DeepChunk, wanted["chunk"])
    sections = await rows(DeepSection, wanted["section"] + [c.section_id for c in chunks.values() if c.section_id])
    doc_ids = [c.document_id for c in chunks.values()] + [s.document_id for s in sections.values()] + wanted["document"]
    docs = await rows(DeepDocument, list(dict.fromkeys(doc_ids)))
    facts = await rows(MemoryItem, wanted["fact"])

    for ref in refs:
        try:
            level, obj_id = parse_ref(ref)
        except ValueError:
            continue
        if level == "chunk":
            chunk = chunks.get(obj_id)
            doc = docs.get(chunk.document_id) if chunk else None
            if chunk is None or chunk.valid_to is not None or not _document_valid(doc):
                continue
            header = (chunk.context_header or "").strip()
            text = f"{header}\n\n{chunk.text}" if header else chunk.text
            out[ref] = IndexPoint(chunk.id, level, text, _chunk_payload(chunk, doc, sections.get(chunk.section_id or "")))
        elif level == "section":
            section = sections.get(obj_id)
            doc = docs.get(section.document_id) if section else None
            if section is None or not (section.summary or "").strip() or not _document_valid(doc):
                continue
            text = f"{section.path or section.title or ''}\n{section.summary}".strip()
            out[ref] = IndexPoint(section.id, level, text, _section_payload(section, doc))
        elif level == "document":
            doc = docs.get(obj_id)
            if not _document_valid(doc) or not (doc.summary or "").strip():
                continue
            out[ref] = IndexPoint(doc.id, level, f"{doc.title or ''}\n{doc.summary}".strip(), _document_payload(doc))
        else:
            item = facts.get(obj_id)
            if (
                item is None
                or item.valid_to is not None
                or item.scope_type != "user"
                or item.memory_type not in INDEXABLE_TYPES
                or not (item.content or "").strip()
            ):
                continue
            out[ref] = IndexPoint(item.id, level, item.content, _fact_payload(item))
    return out


async def fts_search(
    query: str,
    *,
    levels: Sequence[str],
    match: Optional[Dict[str, Any]] = None,
    limit: int = 10,
) -> Optional[List[Hit]]:
    """Postgres text search over the same objects, OR semantics; None when not on Postgres."""
    words = list(dict.fromkeys(w.lower() for w in _FTS_WORD.findall(query or "")))[:16]
    match = match or {}
    async with AsyncSessionLocal() as db:
        if db.get_bind().dialect.name != "postgresql":
            return None
        if not words:
            return []
        english = func.websearch_to_tsquery("english", " or ".join(words))
        hits: List[Hit] = []
        if "chunk" in levels:
            doc_vec = func.to_tsvector(
                "english", func.coalesce(DeepChunk.context_header, "") + " " + func.coalesce(DeepChunk.text, "")
            )
            rank = func.ts_rank_cd(doc_vec, english)
            q = (
                select(DeepChunk, DeepDocument, DeepSection, rank)
                .join(DeepDocument, DeepDocument.id == DeepChunk.document_id)
                .outerjoin(DeepSection, DeepSection.id == DeepChunk.section_id)
                .where(DeepChunk.valid_to.is_(None), DeepDocument.valid_to.is_(None), doc_vec.op("@@")(english))
            )
            for key in ("task_id", "session_key", "document_id"):
                if key in match:
                    q = q.where(getattr(DeepChunk, key) == match[key])
            for chunk, doc, section, score in (await db.execute(q.order_by(rank.desc()).limit(limit))).all():
                hits.append(Hit(chunk.id, float(score), {**_chunk_payload(chunk, doc, section), "level": "chunk"}))
        if "section" in levels:
            sec_vec = func.to_tsvector(
                "english", func.coalesce(DeepSection.title, "") + " " + func.coalesce(DeepSection.summary, "")
            )
            rank = func.ts_rank_cd(sec_vec, english)
            q = (
                select(DeepSection, DeepDocument, rank)
                .join(DeepDocument, DeepDocument.id == DeepSection.document_id)
                .where(DeepDocument.valid_to.is_(None), DeepSection.summary.is_not(None), sec_vec.op("@@")(english))
            )
            for key in ("task_id", "session_key"):
                if key in match:
                    q = q.where(getattr(DeepDocument, key) == match[key])
            if "document_id" in match:
                q = q.where(DeepSection.document_id == match["document_id"])
            for section, doc, score in (await db.execute(q.order_by(rank.desc()).limit(limit))).all():
                hits.append(Hit(section.id, float(score), {**_section_payload(section, doc), "level": "section"}))
        if "document" in levels:
            doc_vec = func.to_tsvector(
                "english", func.coalesce(DeepDocument.title, "") + " " + func.coalesce(DeepDocument.summary, "")
            )
            rank = func.ts_rank_cd(doc_vec, english)
            q = select(DeepDocument, rank).where(
                DeepDocument.valid_to.is_(None), DeepDocument.summary.is_not(None), doc_vec.op("@@")(english)
            )
            for key in ("task_id", "session_key"):
                if key in match:
                    q = q.where(getattr(DeepDocument, key) == match[key])
            if "document_id" in match:
                q = q.where(DeepDocument.id == match["document_id"])
            for doc, score in (await db.execute(q.order_by(rank.desc()).limit(limit))).all():
                hits.append(Hit(doc.id, float(score), {**_document_payload(doc), "level": "document"}))
        if "fact" in levels:
            simple = func.to_tsquery("simple", " | ".join(words))
            fact_vec = func.to_tsvector("simple", func.coalesce(MemoryItem.content, ""))
            rank = func.ts_rank_cd(fact_vec, simple)
            q = select(MemoryItem, rank).where(
                MemoryItem.scope_type == "user",
                MemoryItem.valid_to.is_(None),
                MemoryItem.memory_type.in_(INDEXABLE_TYPES),
                fact_vec.op("@@")(simple),
            )
            for key in ("scope_id", "memory_type"):
                if key in match:
                    q = q.where(getattr(MemoryItem, key) == match[key])
            for item, score in (await db.execute(q.order_by(rank.desc()).limit(limit))).all():
                hits.append(Hit(item.id, float(score), {**_fact_payload(item), "level": "fact"}))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
