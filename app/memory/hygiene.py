"""Canary, heartbeat and system runs leave no trace in user or procedural memory or the registry."""
from __future__ import annotations

from sqlalchemy import or_, select

from app.db.database import AsyncSessionLocal
from app.db.models import MemoryItem, Task, TaskRegistryEntry
from app.notification_policy import is_internal_task

INTERNAL_PROCEDURAL_SCOPES = ("canary", "heartbeat", "system")
# Tokens only canary runs produce; the bare word "canary" is ordinary conversation.
CANARY_MARKERS = ("CANARY_OK", "RMP CANARY", "RMP MEMORY CANARY", "Cushy Gloom")


async def internal_traces():
    """(internal task ids, shared memory rows they left, their registry entries)."""
    async with AsyncSessionLocal() as db:
        tasks = (await db.execute(select(Task.id, Task.goal, Task.task_type))).all()
        internal = {t.id for t in tasks if is_internal_task(t.goal or "", t.task_type or "", [])}
        shared = (
            await db.execute(select(MemoryItem).where(MemoryItem.scope_type.in_(("user", "procedural"))))
        ).scalars().all()
        items = [
            m for m in shared
            if (m.provenance_ref or {}).get("task_id") in internal
            or (m.scope_type == "procedural" and m.scope_id in INTERNAL_PROCEDURAL_SCOPES)
        ]
        entries = (
            await db.execute(select(TaskRegistryEntry).where(TaskRegistryEntry.task_id.in_(internal)))
        ).scalars().all()
    return internal, items, entries


async def canary_text_rows() -> list:
    """Active shared memory rows quoting canary output, whatever their provenance."""
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(MemoryItem.id, MemoryItem.scope_type).where(
                    MemoryItem.scope_type.in_(("user", "procedural")),
                    MemoryItem.valid_to.is_(None),
                    or_(*(MemoryItem.content.contains(marker) for marker in CANARY_MARKERS)),
                )
            )
        ).all()
