#!/usr/bin/env python3
"""Backfill task registry entries for terminal tasks."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/root/.openclaw/rmp")

from sqlalchemy import select

from app.config import get_task_registry_config
from app.db.database import AsyncSessionLocal
from app.db.models import Task
from app.task_registry.indexer import index_terminal_task, TERMINAL_STATUSES


async def main() -> int:
    cfg = get_task_registry_config()
    days = int(cfg.get("backfill_days", 90))
    cutoff = datetime.utcnow() - timedelta(days=days)
    indexed = 0
    errors = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Task)
            .where(
                Task.status.in_(list(TERMINAL_STATUSES)),
                Task.created_at >= cutoff,
            )
            .order_by(Task.created_at.asc())
        )
        tasks = result.scalars().all()
    for task in tasks:
        try:
            entry = await index_terminal_task(task.id)
            if entry:
                indexed += 1
        except Exception as exc:
            errors += 1
            print(f"FAIL {task.id}: {exc}", file=sys.stderr)
    print(f"Backfill complete: scanned={len(tasks)} indexed={indexed} errors={errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
