"""Postgres-backed graph memory: link/unlink/query between memory items."""
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select

from app.db.database import AsyncSessionLocal
from app.db.models import MemoryItem, MemoryLink


async def link_memory(source_id: str, target_id: str, relation: str) -> str:
    async with AsyncSessionLocal() as db:
        for mem_id in (source_id, target_id):
            result = await db.execute(select(MemoryItem).where(MemoryItem.id == mem_id))
            if not result.scalar_one_or_none():
                raise ValueError(f"Memory item not found: {mem_id}")

        existing = await db.execute(
            select(MemoryLink).where(
                MemoryLink.source_id == source_id,
                MemoryLink.target_id == target_id,
                MemoryLink.relation == relation,
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            return row.id

        link_id = str(uuid.uuid4())
        db.add(
            MemoryLink(
                id=link_id,
                source_id=source_id,
                target_id=target_id,
                relation=relation,
            )
        )
        await db.commit()
        return link_id


async def unlink_memory(source_id: str, target_id: str, relation: str) -> bool:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(MemoryLink).where(
                MemoryLink.source_id == source_id,
                MemoryLink.target_id == target_id,
                MemoryLink.relation == relation,
            )
        )
        await db.commit()
        return result.rowcount > 0


async def query_links(
    memory_id: str,
    relation: Optional[str] = None,
    direction: str = "both",
) -> List[Dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        clauses = []
        if direction in ("out", "both"):
            q_out = select(MemoryLink).where(MemoryLink.source_id == memory_id)
            if relation:
                q_out = q_out.where(MemoryLink.relation == relation)
            clauses.append(q_out)
        if direction in ("in", "both"):
            q_in = select(MemoryLink).where(MemoryLink.target_id == memory_id)
            if relation:
                q_in = q_in.where(MemoryLink.relation == relation)
            clauses.append(q_in)

        links: List[MemoryLink] = []
        seen = set()
        for clause in clauses:
            result = await db.execute(clause)
            for link in result.scalars().all():
                if link.id not in seen:
                    links.append(link)
                    seen.add(link.id)

        out: List[Dict[str, Any]] = []
        for link in links:
            direction_label = "out" if link.source_id == memory_id else "in"
            peer_id = link.target_id if direction_label == "out" else link.source_id
            peer_result = await db.execute(
                select(MemoryItem).where(MemoryItem.id == peer_id)
            )
            peer = peer_result.scalar_one_or_none()
            out.append(
                {
                    "link_id": link.id,
                    "relation": link.relation,
                    "direction": direction_label,
                    "peer_id": peer_id,
                    "peer_type": peer.memory_type if peer else None,
                    "peer_content": (peer.content[:200] if peer and peer.content else None),
                }
            )
        return out
