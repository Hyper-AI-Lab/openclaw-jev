"""Remove the legacy procedural rows: old reply text stored as procedures (approved by Kirill 2026-09-30).

Procedural memory holds procedure summaries ("Task: …" with steps and tools), written only for
multi-step tasks that used tools. Rows from before that are old replies, and they crowd Aura's
prompt. Dry run by default; --apply writes a JSON backup of every row to $RMP_DATA_DIR/backups/,
deletes their vectors, then the rows.

    venv/bin/python -m ops.purge_legacy_procedures [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime

from sqlalchemy import delete, or_, select

from app.config import RMP_DATA_DIR
from app.db.database import AsyncSessionLocal
from app.db.models import MemoryItem, MemoryLink
from app.memory.vector_sync import delete_memory_points


def _row(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def _points_naming(row_ids: set) -> list:
    """Older points carry their row id only in the payload, not as point id or vector_ref."""
    from app.memory.vector_sync import _client, memory_collection

    client, name = _client(), memory_collection()
    if not client.collection_exists(name):
        return []
    found, offset = [], None
    while True:
        points, offset = client.scroll(name, limit=1024, offset=offset, with_payload=["provenance"], with_vectors=False)
        found += [str(p.id) for p in points if ((p.payload or {}).get("provenance") or {}).get("memory_id") in row_ids]
        if offset is None:
            return found


async def legacy_procedures() -> list:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(select(MemoryItem).where(MemoryItem.scope_type == "procedural"))
        ).scalars().all()
    return [m for m in rows if not (m.content or "").startswith("Task:")]


async def main(apply: bool) -> None:
    doomed = await legacy_procedures()
    pools: dict = {}
    for m in doomed:
        pools[m.scope_id] = pools.get(m.scope_id, 0) + 1
    print(f"legacy procedural rows: {len(doomed)} by pool {pools}")
    for m in doomed[:5]:
        print("  e.g.", " ".join((m.content or "").split())[:100])
    if not apply:
        print("dry run: nothing deleted (use --apply)")
        return
    if not doomed:
        return
    backup_dir = os.path.join(RMP_DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, f"purge-legacy-procedures-{datetime.utcnow():%Y%m%dT%H%M%SZ}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"memory_items": [_row(m) for m in doomed]}, fh, default=str)
    os.chmod(path, 0o600)
    print(f"backup: {path}")
    ids = [m.id for m in doomed]
    named = await asyncio.to_thread(_points_naming, set(ids))
    vectors = await asyncio.to_thread(
        delete_memory_points, ids + [(m.provenance_ref or {}).get("vector_ref") for m in doomed] + named
    )
    async with AsyncSessionLocal() as db:
        await db.execute(delete(MemoryLink).where(or_(MemoryLink.source_id.in_(ids), MemoryLink.target_id.in_(ids))))
        await db.execute(delete(MemoryItem).where(MemoryItem.id.in_(ids)))
        await db.commit()
    print(f"deleted: {len(ids)} rows, {vectors} vector ids")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    asyncio.run(main(parser.parse_args().apply))
