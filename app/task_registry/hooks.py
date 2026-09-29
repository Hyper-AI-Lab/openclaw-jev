"""Hooks to index tasks when they reach terminal state."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("rmp.task_registry.hooks")


def schedule_terminal_index(task_id: str) -> None:
    if not task_id:
        return
    try:
        from app.task_registry.indexer import index_terminal_task

        asyncio.create_task(index_terminal_task(task_id))
    except RuntimeError:
        # No running loop (sync context) — run inline
        try:
            asyncio.get_event_loop().run_until_complete(
                __import__(
                    "app.task_registry.indexer", fromlist=["index_terminal_task"]
                ).index_terminal_task(task_id)
            )
        except Exception as exc:
            logger.warning("Terminal index inline failed for %s: %s", task_id, exc)


async def index_terminal_task_async(task_id: str) -> None:
    """Queue a finished task for the registry index; the vector outbox drains it."""
    from app.memory.vector_sync import enqueue_registry_index

    try:
        await enqueue_registry_index(task_id)
    except Exception as exc:
        logger.warning("Registry index enqueue failed for %s: %s", task_id, exc)
