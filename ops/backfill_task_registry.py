#!/usr/bin/env python3
"""Backfill task registry entries for terminal tasks."""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/root/.openclaw/rmp")

from sqlalchemy import select

from app.config import get_task_registry_config
from app.db.database import AsyncSessionLocal, engine
from app.db.models import Task, TaskRegistryEntry
from app.task_registry.indexer import index_terminal_task, TERMINAL_STATUSES


async def _missing_task_ids(cutoff: datetime, limit: int | None) -> list[str]:
    async with AsyncSessionLocal() as db:
        existing = select(TaskRegistryEntry.task_id)
        q = (
            select(Task.id)
            .where(
                Task.status.in_(list(TERMINAL_STATUSES)),
                Task.created_at >= cutoff,
                Task.id.not_in(existing),
            )
            .order_by(Task.created_at.asc())
        )
        if limit is not None and limit > 0:
            q = q.limit(limit)
        result = await db.execute(q)
        return [row[0] for row in result.all()]


async def _all_task_ids(cutoff: datetime, limit: int | None) -> list[str]:
    async with AsyncSessionLocal() as db:
        q = (
            select(Task.id)
            .where(
                Task.status.in_(list(TERMINAL_STATUSES)),
                Task.created_at >= cutoff,
            )
            .order_by(Task.created_at.asc())
        )
        if limit is not None and limit > 0:
            q = q.limit(limit)
        result = await db.execute(q)
        return [row[0] for row in result.all()]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--missing-only",
        action="store_true",
        default=True,
        help="Only index terminal tasks lacking a registry entry (default)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Re-index all terminal tasks in the backfill window",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max tasks to process (0 = no limit)",
    )
    parser.add_argument(
        "--sleep-sec",
        type=float,
        default=0.0,
        help="Pause between successful indexes (quota pacing)",
    )
    args = parser.parse_args()

    cfg = get_task_registry_config()
    days = int(cfg.get("backfill_days", 90))
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    limit = args.limit if args.limit > 0 else None
    missing_only = not args.all

    task_ids = (
        await _missing_task_ids(cutoff, limit)
        if missing_only
        else await _all_task_ids(cutoff, limit)
    )
    mode = "missing-only" if missing_only else "all"
    print(f"Backfill start: mode={mode} candidates={len(task_ids)} days={days}")

    indexed = 0
    errors = 0
    for i, task_id in enumerate(task_ids, 1):
        try:
            entry = await index_terminal_task(task_id)
            if entry:
                indexed += 1
            if i % 25 == 0 or i == len(task_ids):
                print(f"progress {i}/{len(task_ids)} indexed={indexed} errors={errors}")
            if args.sleep_sec > 0:
                await asyncio.sleep(args.sleep_sec)
        except Exception as exc:
            errors += 1
            print(f"FAIL {task_id}: {exc}", file=sys.stderr)
            # Brief pause on failure (often rate limit); keep going.
            await asyncio.sleep(max(args.sleep_sec, 1.0))

    print(f"Backfill complete: scanned={len(task_ids)} indexed={indexed} errors={errors}")
    await engine.dispose()
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
