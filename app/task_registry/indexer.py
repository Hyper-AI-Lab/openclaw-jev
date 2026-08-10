"""Index terminal tasks into Postgres registry + Qdrant."""
from __future__ import annotations

import logging
from typing import Optional

from app.config import get_task_registry_config
from app.task_registry.summary import upsert_registry_entry
from app.task_registry.vector_store import upsert_task_vector

logger = logging.getLogger("rmp.task_registry.indexer")

TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "stopped_by_user", "cancelled", "compensated"}
)


async def index_terminal_task(task_id: str) -> Optional[str]:
    cfg = get_task_registry_config()
    if not cfg.get("enabled", True):
        return None
    from app.task_registry.summary import build_task_summary

    summary = await build_task_summary(task_id)
    if not summary:
        return None
    if summary.get("terminal_status") not in TERMINAL_STATUSES:
        logger.debug("Skip registry index for non-terminal task %s", task_id)
        return None
    point_id = upsert_task_vector(task_id, summary)
    entry_id = await upsert_registry_entry(task_id, vector_point_id=point_id)
    logger.info("Indexed task %s into registry (entry=%s)", task_id[:8], entry_id)
    return entry_id
