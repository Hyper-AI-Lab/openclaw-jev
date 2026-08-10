#!/usr/bin/env python3
"""Terminate stale Temporal workflows to relieve SQLite pressure on dev server."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/root/.openclaw/rmp")

from sqlalchemy import select
from temporalio.client import Client, WorkflowExecutionStatus

from app.db.database import AsyncSessionLocal
from app.db.models import Task

logger = logging.getLogger("rmp.temporal_purge")

TERMINAL_TASK = frozenset(
    {"failed", "completed", "stopped_by_user", "cancelled", "compensated"}
)


async def purge_running(
    *, max_age_minutes: int = 0, dry_run: bool = False, force_recovery: bool = False
) -> dict:
    stats = {"scanned": 0, "terminated": 0, "skipped": 0, "errors": 0, "tasks_failed": 0}
    client = await Client.connect("localhost:7233")
    now = datetime.now(timezone.utc)
    cutoff = None
    if max_age_minutes > 0:
        from datetime import timedelta

        cutoff = now - timedelta(minutes=max_age_minutes)

    failed_task_ids: set[str] = set()

    async for wf in client.list_workflows('ExecutionStatus="Running"'):
        stats["scanned"] += 1
        start = wf.start_time
        if start and start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if not force_recovery and cutoff and start and start >= cutoff:
            stats["skipped"] += 1
            continue

        task_id = None
        if wf.id.startswith("workflow-"):
            task_id = wf.id.removeprefix("workflow-")
        elif wf.id.startswith("intake-"):
            task_id = None
        elif "-plan-" in wf.id:
            task_id = wf.id.split("-plan-", 1)[0]

        terminate = force_recovery
        if not terminate:
            if wf.id.startswith("intake-"):
                terminate = True
            elif task_id is None:
                terminate = True
            else:
                async with AsyncSessionLocal() as db:
                    row = await db.execute(select(Task).where(Task.id == task_id))
                    task = row.scalar_one_or_none()
                    if task is None or task.status in TERMINAL_TASK:
                        terminate = True

        if not terminate:
            stats["skipped"] += 1
            continue

        if dry_run:
            logger.info("Would terminate %s", wf.id)
            stats["terminated"] += 1
            if task_id:
                failed_task_ids.add(task_id)
            continue

        try:
            handle = client.get_workflow_handle(wf.id)
            await handle.terminate("Temporal purge: stale or orphaned workflow")
            stats["terminated"] += 1
            if task_id:
                failed_task_ids.add(task_id)
            logger.info("Terminated %s", wf.id)
        except Exception as exc:
            err = str(exc).lower()
            if "already completed" in err or "not found" in err:
                stats["terminated"] += 1
                if task_id:
                    failed_task_ids.add(task_id)
                continue
            stats["errors"] += 1
            logger.warning("Failed to terminate %s: %s", wf.id, exc)

    if failed_task_ids and not dry_run:
        async with AsyncSessionLocal() as db:
            for tid in failed_task_ids:
                row = await db.execute(select(Task).where(Task.id == tid))
                task = row.scalar_one_or_none()
                if task and task.status not in TERMINAL_TASK:
                    task.status = "failed"
                    stats["tasks_failed"] += 1
            await db.commit()

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge stale Temporal workflows")
    parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=0,
        help="Only terminate workflows older than N minutes (0 = all eligible)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force-recovery",
        action="store_true",
        help="Full recovery: terminate all running workflows and fail linked tasks",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    stats = asyncio.run(
        purge_running(
            max_age_minutes=args.max_age_minutes,
            dry_run=args.dry_run,
            force_recovery=args.force_recovery,
        )
    )
    print(f"Purge complete: {stats}")
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
