"""Remove canary, system and heartbeat traces from shared memory (approved by Kirill 2026-09-29).

Deletes user and procedural memory rows whose source task is internal, the registry entries of
internal tasks, and their vectors. Dry run by default; --apply deletes after writing a JSON backup
of every row it removes to $RMP_DATA_DIR/backups/.

    venv/bin/python -m ops.purge_internal_memory [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime

from sqlalchemy import delete

from app.config import RMP_DATA_DIR, get_task_registry_config
from app.db.database import AsyncSessionLocal
from app.db.models import MemoryItem, TaskRegistryEntry
from app.memory.hygiene import internal_traces
from app.notification_policy import is_internal_task


def _row(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def _internal_memory_point(payload: dict, internal: set) -> bool:
    if payload.get("scope_type") not in ("user", "procedural"):
        return False
    text = str(payload.get("data") or "")
    source = (payload.get("provenance") or {}).get("task_id")
    return source in internal or "CANARY" in text.upper() or "Cushy Gloom" in text


def _internal_registry_point(payload: dict, internal: set) -> bool:
    return payload.get("task_id") in internal or is_internal_task(
        str(payload.get("intent_snippet") or ""), str(payload.get("process_type") or ""), []
    )


def _sweep_shared_internal_vectors(internal: set, apply: bool) -> dict:
    """Internal points in the shared indexes, including ones no row references."""
    from qdrant_client.http import models as rest

    from app.config import get_vector_memory_config
    from app.task_registry.vector_store import _get_qdrant_client

    client = _get_qdrant_client()
    targets = {
        get_vector_memory_config().get("collection_name"): _internal_memory_point,
        get_task_registry_config().get("collection_name", "rmp_task_registry"): _internal_registry_point,
    }
    counts = {}
    for collection, is_internal_point in targets.items():
        doomed, offset = [], None
        while True:
            points, offset = client.scroll(
                collection, limit=512, offset=offset, with_payload=True, with_vectors=False
            )
            doomed += [p.id for p in points if is_internal_point(p.payload or {}, internal)]
            if offset is None:
                break
        if apply:
            for start in range(0, len(doomed), 256):
                client.delete(
                    collection_name=collection,
                    points_selector=rest.PointIdsList(points=doomed[start:start + 256]),
                    wait=True,
                )
        counts[collection] = len(doomed)
    return counts


def _delete_vectors(items, entries) -> tuple[int, int]:
    from qdrant_client.http import models as rest

    from app.memory.vector import get_vector_service
    from app.task_registry.vector_store import _get_qdrant_client

    svc = get_vector_service()
    memory_deleted = sum(
        1 for m in items
        if (m.provenance_ref or {}).get("vector_ref") and svc.delete(m.provenance_ref["vector_ref"])
    )
    point_ids = [e.vector_point_id for e in entries if e.vector_point_id]
    if point_ids:
        client = _get_qdrant_client()
        collection = get_task_registry_config().get("collection_name", "rmp_task_registry")
        for start in range(0, len(point_ids), 256):
            client.delete(
                collection_name=collection,
                points_selector=rest.PointIdsList(points=point_ids[start:start + 256]),
                wait=True,
            )
    return memory_deleted, len(point_ids)


async def main(apply: bool) -> None:
    internal, items, entries = await internal_traces()
    by_scope: dict = {}
    for m in items:
        key = f"{m.scope_type}/{m.memory_type}"
        by_scope[key] = by_scope.get(key, 0) + 1
    print(f"internal tasks: {len(internal)}")
    print(f"memory rows to delete: {len(items)} {by_scope}")
    print(f"registry entries to delete: {len(entries)}")
    swept = await asyncio.to_thread(_sweep_shared_internal_vectors, internal, apply)
    print(f"shared internal vectors {'deleted' if apply else 'to delete'}: {swept}")
    if not apply:
        print("dry run: nothing deleted (use --apply)")
        return
    if not items and not entries:
        return

    backup_dir = os.path.join(RMP_DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, f"purge-internal-memory-{datetime.utcnow():%Y%m%dT%H%M%SZ}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"memory_items": [_row(m) for m in items],
                   "task_registry_entries": [_row(e) for e in entries]}, fh, default=str)
    os.chmod(path, 0o600)
    print(f"backup: {path}")

    memory_vectors, registry_vectors = await asyncio.to_thread(_delete_vectors, items, entries)
    async with AsyncSessionLocal() as db:
        if items:
            await db.execute(delete(MemoryItem).where(MemoryItem.id.in_([m.id for m in items])))
        if entries:
            await db.execute(delete(TaskRegistryEntry).where(TaskRegistryEntry.id.in_([e.id for e in entries])))
        await db.commit()
    print(f"deleted: {len(items)} memory rows ({memory_vectors} vectors), "
          f"{len(entries)} registry entries ({registry_vectors} vectors)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    asyncio.run(main(parser.parse_args().apply))
