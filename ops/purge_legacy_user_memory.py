"""Remove the legacy user-memory rows: workspace-file chunks and "Site referenced" notes (approved by Kirill 2026-09-30).

User memory holds facts extracted from Kirill's conversations. Rows from before that are chunks
of Aura's workspace files seeded as facts (USER.md, AGENTS.md, SOUL.md, MEMORY.md, daily notes)
and pages the removed promotion heuristic recorded; recall quoted them as standing policy. Dry run
by default; --apply writes a JSON backup of the rows and their links to $RMP_DATA_DIR/backups/,
deletes their deep-memory points, then the rows.

    venv/bin/python -m ops.purge_legacy_user_memory [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, or_, select

from app.config import RMP_DATA_DIR
from app.db.database import AsyncSessionLocal
from app.db.models import MemoryItem, MemoryLink
from app.deep_memory import index


def _row(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def _origin(m: MemoryItem) -> Optional[str]:
    provenance = m.provenance_ref or {}
    if provenance.get("source") == "workspace_seed":
        return os.path.basename(str(provenance.get("path") or "workspace"))
    if "promotion_stage" in provenance:
        return "promotion heuristic"
    return None


async def legacy_user_rows() -> list:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(MemoryItem).where(MemoryItem.scope_type == "user"))).scalars().all()
    return [m for m in rows if _origin(m)]


async def main(apply: bool) -> None:
    doomed = await legacy_user_rows()
    print(f"legacy user rows: {len(doomed)} by origin {dict(Counter(_origin(m) for m in doomed))}")
    for m in doomed[:5]:
        print("  e.g.", " ".join((m.content or "").split())[:100])
    if not apply:
        print("dry run: nothing deleted (use --apply)")
        return
    if not doomed:
        return
    ids = [m.id for m in doomed]
    touching = or_(MemoryLink.source_id.in_(ids), MemoryLink.target_id.in_(ids))
    async with AsyncSessionLocal() as db:
        links = (await db.execute(select(MemoryLink).where(touching))).scalars().all()
    backup_dir = os.path.join(RMP_DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, f"purge-legacy-user-memory-{datetime.utcnow():%Y%m%dT%H%M%SZ}.json")
    with open(path, "w", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)) as fh:
        json.dump({"memory_items": [_row(m) for m in doomed], "memory_links": [_row(link) for link in links]},
                  fh, default=str, ensure_ascii=False)
    print(f"backup: {path}")
    points = await asyncio.to_thread(index.delete_points, ids)
    async with AsyncSessionLocal() as db:
        await db.execute(delete(MemoryLink).where(touching))
        await db.execute(delete(MemoryItem).where(MemoryItem.id.in_(ids)))
        await db.commit()
    print(f"deleted: {len(ids)} rows, {len(links)} links, {points} deep-memory point ids")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    asyncio.run(main(parser.parse_args().apply))
