"""Postgres holds memory; Qdrant indexes it (transactional outbox + reconciler).

Every indexable memory row and every registry entry gets a `vector_outbox` row in the
transaction that writes it. The drainer embeds and upserts with point id = row id, so a
retry overwrites instead of duplicating. The reconciler diffs Postgres with both indexes:
missing rows and finished user tasks without a registry entry are queued, points no row
references are deleted.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select

from app.config import get_task_registry_config, get_vector_memory_config, is_vector_memory_enabled
from app.db.database import AsyncSessionLocal
from app.db.models import MemoryItem, Task, TaskRegistryEntry, VectorOutbox
from app.memory.vector import INDEXABLE_TYPES, embed_query_text, scope_to_mem0_ids
from app.notification_policy import is_internal_task
from app.orchestrator.decision_engine import TERMINAL_STATUSES

logger = logging.getLogger("rmp.vector_sync")

DRAIN_INTERVAL_SEC = 15
DRAIN_BATCH = 50
RECONCILE_INTERVAL_SEC = 24 * 3600
MAX_BACKOFF_SEC = 3600
# Refuse a reconcile that would delete most of an index: a failed Postgres read looks the same.
MAX_ORPHAN_SHARE = 0.5
# A task that just ended is indexed by its own run first.
UNINDEXED_SETTLE_MINUTES = 10


def _client():
    from app.task_registry.vector_store import _get_qdrant_client

    return _get_qdrant_client()


def memory_collection() -> str:
    return get_vector_memory_config().get("collection_name") or "rmp_memories"


def registry_collection() -> str:
    return get_task_registry_config().get("collection_name", "rmp_task_registry")


def memory_point_payload(item: MemoryItem) -> Dict[str, Any]:
    """The payload Mem0 writes, so Mem0 search reads points written here."""
    ids = scope_to_mem0_ids(item.scope_type, item.scope_id)
    payload: Dict[str, Any] = {
        "data": item.content,
        "hash": hashlib.md5(item.content.encode("utf-8")).hexdigest(),
        "created_at": (item.created_at or datetime.utcnow()).isoformat(),
        "memory_type": item.memory_type,
        "scope_type": item.scope_type,
        "scope_id": item.scope_id,
        "provenance": {**(item.provenance_ref or {}), "memory_id": item.id},
        "role": "user",
        **{k: v for k, v in ids.items() if k in ("user_id", "agent_id", "run_id")},
    }
    if ids.get("scope_id"):
        payload["procedural_scope_id"] = ids["scope_id"]
    return payload


def _ensure_memory_collection() -> None:
    from qdrant_client.http import models as rest

    client = _client()
    name = memory_collection()
    if not client.collection_exists(name):
        dims = int(get_vector_memory_config().get("embedding_dims") or 1536)
        client.create_collection(
            name, vectors_config=rest.VectorParams(size=dims, distance=rest.Distance.COSINE)
        )


def _upsert_memory_point(item: MemoryItem) -> None:
    from qdrant_client.http import models as rest

    vector = embed_query_text(item.content, get_vector_memory_config())
    if not vector:
        raise RuntimeError("embedder returned no vector")
    _ensure_memory_collection()
    _client().upsert(
        collection_name=memory_collection(),
        points=[rest.PointStruct(id=item.id, vector=vector, payload=memory_point_payload(item))],
        wait=True,
    )


def _delete_points(collection: str, point_ids: Iterable[Optional[str]]) -> int:
    from qdrant_client.http import models as rest

    ids = [p for p in dict.fromkeys(point_ids) if p]
    for start in range(0, len(ids), 256):
        _client().delete(
            collection_name=collection,
            points_selector=rest.PointIdsList(points=ids[start:start + 256]),
            wait=True,
        )
    return len(ids)


def delete_memory_points(point_ids: Iterable[Optional[str]]) -> int:
    return _delete_points(memory_collection(), point_ids)


async def enqueue_registry_index(task_id: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add(VectorOutbox(kind="registry", ref_id=task_id))
        await db.commit()


async def _apply(db, row: VectorOutbox) -> None:
    if row.kind == "memory":
        item = await db.get(MemoryItem, row.ref_id)
        if item is None or item.valid_to is not None or item.memory_type not in INDEXABLE_TYPES:
            legacy = (item.provenance_ref or {}).get("vector_ref") if item is not None else None
            await asyncio.to_thread(delete_memory_points, [row.ref_id, legacy])
            return
        await asyncio.to_thread(_upsert_memory_point, item)
    elif row.kind == "registry":
        from app.task_registry.indexer import index_terminal_task

        await index_terminal_task(row.ref_id, require_vector=True)
    else:
        raise ValueError(f"unknown outbox kind {row.kind!r}")


async def drain_once(limit: int = DRAIN_BATCH) -> Dict[str, int]:
    stats = {"done": 0, "failed": 0}
    now = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(VectorOutbox)
                .where(VectorOutbox.done_at.is_(None), VectorOutbox.next_attempt_at <= now)
                .order_by(VectorOutbox.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for row in rows:
            try:
                await _apply(db, row)
            except Exception as exc:
                row.attempts = (row.attempts or 0) + 1
                row.last_error = str(exc)[:500]
                backoff = min(MAX_BACKOFF_SEC, 30 * 2 ** min(row.attempts, 7))
                row.next_attempt_at = datetime.utcnow() + timedelta(seconds=backoff)
                stats["failed"] += 1
            else:
                row.done_at = datetime.utcnow()
                row.last_error = None
                stats["done"] += 1
        await db.commit()
    return stats


def _scroll(collection: str, key: Optional[str]) -> List[Tuple[str, Any]]:
    """(point id, payload[key]) for every point; key None means ids only."""
    client = _client()
    if not client.collection_exists(collection):
        return []
    out, offset = [], None
    while True:
        points, offset = client.scroll(
            collection, limit=1024, offset=offset, with_payload=[key] if key else False, with_vectors=False
        )
        out += [(str(p.id), (p.payload or {}).get(key) if key else None) for p in points]
        if offset is None:
            return out


def _guarded(orphans: List[str], total: int, collection: str) -> List[str]:
    if total >= 100 and len(orphans) > MAX_ORPHAN_SHARE * total:
        logger.error(
            "Vector reconcile refused: %d of %d points in %s look orphaned", len(orphans), total, collection
        )
        return []
    return orphans


async def reconcile(apply: bool = True) -> Dict[str, Any]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(MemoryItem.id, MemoryItem.provenance_ref).where(
                    MemoryItem.memory_type.in_(INDEXABLE_TYPES), MemoryItem.valid_to.is_(None)
                )
            )
        ).all()
        entries = {tid for (tid,) in (await db.execute(select(TaskRegistryEntry.task_id))).all()}
        now = datetime.utcnow()
        ended = (
            await db.execute(
                select(Task.id, Task.goal, Task.task_type).where(
                    Task.status.in_(TERMINAL_STATUSES),
                    Task.created_at >= now - timedelta(days=int(get_task_registry_config().get("backfill_days", 90))),
                    Task.updated_at < now - timedelta(minutes=UNINDEXED_SETTLE_MINUTES),
                )
            )
        ).all()
        pending = {
            (kind, ref)
            for kind, ref in (
                await db.execute(
                    select(VectorOutbox.kind, VectorOutbox.ref_id).where(VectorOutbox.done_at.is_(None))
                )
            ).all()
        }
    active = {rid: (prov or {}).get("vector_ref") for rid, prov in rows}
    legacy = {ref: rid for rid, ref in active.items() if ref}

    points = await asyncio.to_thread(_scroll, memory_collection(), "provenance")
    indexed, orphans = set(), []
    for pid, provenance in points:
        owner = pid if pid in active else legacy.get(pid)
        if owner is None and isinstance(provenance, dict) and provenance.get("memory_id") in active:
            owner = provenance["memory_id"]
        if owner:
            indexed.add(owner)
        else:
            orphans.append(pid)
    missing = [rid for rid in active if rid not in indexed and ("memory", rid) not in pending]
    orphans = _guarded(orphans, len(points), memory_collection())

    reg_points = {pid for pid, _ in await asyncio.to_thread(_scroll, registry_collection(), None)}
    reg_missing = [tid for tid in entries if tid not in reg_points and ("registry", tid) not in pending]
    reg_orphans = _guarded(sorted(reg_points - entries), len(reg_points), registry_collection())
    # A task can end on a path that never queues its registry entry.
    unindexed = [
        t.id for t in ended
        if t.id not in entries
        and ("registry", t.id) not in pending
        and not is_internal_task(t.goal or "", t.task_type or "", [])
    ]

    stats = {
        "memory_rows": len(active),
        "memory_points": len(points),
        "memory_missing": len(missing),
        "memory_orphans": len(orphans),
        "registry_entries": len(entries),
        "registry_points": len(reg_points),
        "registry_missing": len(reg_missing),
        "registry_orphans": len(reg_orphans),
        "registry_unindexed": len(unindexed),
        "applied": apply,
    }
    if apply:
        async with AsyncSessionLocal() as db:
            db.add_all([VectorOutbox(kind="memory", ref_id=rid) for rid in missing])
            db.add_all([VectorOutbox(kind="registry", ref_id=tid) for tid in reg_missing + unindexed])
            await db.commit()
        await asyncio.to_thread(delete_memory_points, orphans)
        await asyncio.to_thread(_delete_points, registry_collection(), reg_orphans)
    logger.info("Vector reconcile: %s", stats)
    return stats


async def vector_outbox_loop(stop_event: asyncio.Event) -> None:
    last_reconcile = time.monotonic()
    logger.info("Vector outbox loop started (drain every %ss)", DRAIN_INTERVAL_SEC)
    while not stop_event.is_set():
        try:
            if is_vector_memory_enabled():
                await drain_once()
                if time.monotonic() - last_reconcile >= RECONCILE_INTERVAL_SEC:
                    await reconcile()
                    last_reconcile = time.monotonic()
        except Exception:
            logger.exception("Vector outbox loop error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=DRAIN_INTERVAL_SEC)
            break
        except asyncio.TimeoutError:
            pass
    logger.info("Vector outbox loop stopped")
