#!/usr/bin/env python3
"""Daily workflow janitor — terminate long-running orphan Temporal workflows."""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow `python ops/workflow_janitor.py` without relying solely on systemd PYTHONPATH.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import select
from temporalio.client import Client, WorkflowExecutionStatus

from app.db.database import AsyncSessionLocal, engine
from app.db.models import Task

logger = logging.getLogger("rmp.janitor")

JANITOR_MAX_AGE_HOURS = 24
TERMINAL_TASK_STATUSES = frozenset(
    {"failed", "completed", "stopped_by_user", "cancelled", "compensated"}
)


async def janitor_once(max_age_hours: int = JANITOR_MAX_AGE_HOURS) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    stats = {"scanned": 0, "terminated": 0, "errors": 0}

    client = await Client.connect("localhost:7233")
    try:
        async for wf in client.list_workflows('ExecutionStatus="Running"'):
            stats["scanned"] += 1
            start = wf.start_time
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if start >= cutoff:
                continue

            task_id = None
            if wf.id.startswith("workflow-"):
                task_id = wf.id.removeprefix("workflow-")
            elif "-plan-" in wf.id:
                task_id = wf.id.split("-plan-", 1)[0]

            should_terminate = task_id is None
            if task_id:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(Task).where(Task.id == task_id))
                    task = result.scalar_one_or_none()
                    if task is None or task.status in TERMINAL_TASK_STATUSES:
                        should_terminate = True

            if not should_terminate:
                continue

            try:
                handle = client.get_workflow_handle(wf.id)
                await handle.terminate(
                    f"Janitor: running >{max_age_hours}h with terminal/missing task"
                )
                stats["terminated"] += 1
                logger.info("Janitor terminated %s", wf.id)
            except Exception as exc:
                stats["errors"] += 1
                logger.warning("Janitor failed to terminate %s: %s", wf.id, exc)
    finally:
        # Avoid asyncpg/grpc GIL abort during interpreter finalization under systemd.
        await engine.dispose()

    return stats


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="RMP workflow janitor")
    parser.add_argument(
        "--max-age-hours",
        type=int,
        default=JANITOR_MAX_AGE_HOURS,
        help="Terminate running workflows older than N hours when task missing/terminal",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    stats = asyncio.run(janitor_once(max_age_hours=args.max_age_hours))
    print(f"Janitor complete: {stats}")
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
