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
    from app.task_registry.indexer import index_terminal_task

    try:
        await index_terminal_task(task_id)
    except Exception as exc:
        logger.warning("Terminal index failed for %s: %s", task_id, exc)
