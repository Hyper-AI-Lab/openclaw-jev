"""Process-scoped memory routing per the RMP architecture plan."""
import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select

from app.config import get_vector_memory_config, is_vector_memory_enabled
from app.db.database import AsyncSessionLocal
from app.db.models import MemoryItem, VectorOutbox
from app.memory.policy import apply_write_policy, redact_secrets
from app.memory.vector import INDEXABLE_TYPES, get_vector_service

logger = logging.getLogger("rmp.memory")

VECTOR_SEARCH_TIMEOUT_SEC = 20.0
# Cap concurrent vector/DB memory legs so parallel scope reads don't stampede embeds.
MEMORY_READ_CONCURRENCY = 3


async def _vector_search_bounded(
    svc: Any,
    scope_type: str,
    scope_id: str,
    query: str,
    limit: int,
    memory_type: Optional[str],
) -> Optional[List[Dict[str, Any]]]:
    """Vector hits; None when the index or embedder did not answer."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                svc.search,
                scope_type,
                scope_id,
                query,
                limit,
                memory_type,
            ),
            timeout=VECTOR_SEARCH_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Vector search timed out after %ss for %s/%s",
            VECTOR_SEARCH_TIMEOUT_SEC,
            scope_type,
            scope_id,
        )
        return None
    except Exception as exc:
        logger.warning(
            "Vector search failed for %s/%s: %s",
            scope_type,
            scope_id,
            exc,
        )
        return None


_FTS_WORD = re.compile(r"[^\W_]{3,}", re.UNICODE)


async def _fts_search(
    scope_type: str,
    scope_id: str,
    query: str,
    limit: int,
    memory_type: Optional[str],
) -> List[Dict[str, Any]]:
    """Keyword recall from Postgres for when the vector index or embedder is down."""
    words = list(dict.fromkeys(w.lower() for w in _FTS_WORD.findall(query or "")))[:12]
    if not words:
        return []
    async with AsyncSessionLocal() as db:
        if db.get_bind().dialect.name != "postgresql":
            return []
        tsquery = func.to_tsquery("simple", " | ".join(words))
        document = func.to_tsvector("simple", func.coalesce(MemoryItem.content, ""))
        q = select(MemoryItem).where(
            MemoryItem.scope_type == scope_type,
            MemoryItem.scope_id == scope_id,
            MemoryItem.valid_to.is_(None),
            document.op("@@")(tsquery),
        )
        if memory_type:
            q = q.where(MemoryItem.memory_type == memory_type)
        q = q.order_by(func.ts_rank(document, tsquery).desc()).limit(limit)
        rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id": m.id,
            "memory_type": m.memory_type,
            "content": redact_secrets(m.content or ""),
            "confidence": m.confidence,
            "source": "postgres_fts",
        }
        for m in rows
    ]


class MemoryRouter:
    """Routes memory reads/writes by scope (process, task, user, procedural)."""

    @staticmethod
    async def write(
        scope_type: str,
        scope_id: str,
        memory_type: str,
        content: str,
        provenance: Optional[Dict[str, Any]] = None,
        confidence: int = 100,
    ) -> str:
        import uuid

        allowed, reason, redacted = apply_write_policy(
            scope_type, memory_type, content, confidence
        )
        if not allowed:
            logger.warning(
                "Memory write rejected: scope=%s type=%s reason=%s",
                scope_type,
                memory_type,
                reason,
            )
            raise ValueError(f"Memory write rejected: {reason}")

        mem_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(
                MemoryItem(
                    id=mem_id,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    memory_type=memory_type,
                    content=redacted,
                    provenance_ref=provenance,
                    confidence=confidence,
                    valid_from=datetime.utcnow(),
                )
            )
            if is_vector_memory_enabled() and memory_type in INDEXABLE_TYPES:
                db.add(VectorOutbox(kind="memory", ref_id=mem_id))
            await db.commit()
        return mem_id

    @staticmethod
    async def read(
        scope_type: str,
        scope_id: str,
        memory_type: Optional[str] = None,
        limit: int = 20,
        query: Optional[str] = None,
        skip_vector: bool = False,
    ) -> List[Dict[str, Any]]:
        async with AsyncSessionLocal() as db:
            q = select(MemoryItem).where(
                MemoryItem.scope_type == scope_type,
                MemoryItem.scope_id == scope_id,
            )
            if memory_type:
                q = q.where(MemoryItem.memory_type == memory_type)
            q = q.order_by(MemoryItem.created_at.desc()).limit(limit)
            result = await db.execute(q)
            pg_items = [
                {
                    "id": m.id,
                    "memory_type": m.memory_type,
                    "content": redact_secrets(m.content or ""),
                    "confidence": m.confidence,
                    "source": "postgres",
                }
                for m in result.scalars().all()
            ]

        if query and not skip_vector:
            try:
                vm_cfg = get_vector_memory_config()
                vector_limit = int(vm_cfg.get("semantic_recall_limit", 5))
                indexed_type = memory_type if memory_type in INDEXABLE_TYPES else None
                vector_hits = None
                if is_vector_memory_enabled():
                    vector_hits = await _vector_search_bounded(
                        get_vector_service(), scope_type, scope_id, query, vector_limit, indexed_type
                    )
                if vector_hits is None:
                    vector_hits = await _fts_search(
                        scope_type, scope_id, query, vector_limit, indexed_type
                    )
                seen = {item["content"][:200] for item in pg_items}
                merged = list(pg_items)
                for hit in vector_hits:
                    hit["content"] = redact_secrets(hit.get("content", ""))
                    key = hit["content"][:200]
                    if key not in seen:
                        merged.append(hit)
                        seen.add(key)
                return merged[: limit + vector_limit]
            except Exception as e:
                logger.warning(
                    "Vector recall skipped for %s/%s: %s",
                    scope_type,
                    scope_id,
                    e,
                )

        return pg_items

    @staticmethod
    async def search_semantic(
        scope_type: str,
        scope_id: str,
        query: str,
        limit: int = 5,
        memory_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        hits = None
        if is_vector_memory_enabled():
            hits = await _vector_search_bounded(
                get_vector_service(), scope_type, scope_id, query, limit, memory_type
            )
        if hits is None:
            hits = await _fts_search(scope_type, scope_id, query, limit, memory_type)
        return hits

    @staticmethod
    async def read_ordered(
        process_run_id: str,
        task_id: Optional[str] = None,
        user_scope_id: str = "default",
        process_type: str = "generic",
        query: Optional[str] = None,
        limit: int = 20,
        skip_vector: bool = False,
    ) -> List[Dict[str, Any]]:
        """Report §6.2 read order: process → working → procedural → semantic → graph.

        Scope reads run concurrently (capped) but merge order is preserved.
        """
        from app.memory.graph import query_links

        merged: List[Dict[str, Any]] = []
        seen: set = set()
        proc_scope = (process_type or "generic").strip() or "generic"

        def _add(items: List[Dict[str, Any]]) -> None:
            for item in items:
                key = (item.get("memory_type"), item.get("content", "")[:200])
                if key not in seen:
                    merged.append(item)
                    seen.add(key)

        async def _safe_read(*args, **kwargs) -> List[Dict[str, Any]]:
            try:
                if skip_vector:
                    kwargs = {**kwargs, "skip_vector": True}
                return await MemoryRouter.read(*args, **kwargs)
            except Exception as exc:
                logger.warning("Memory read skipped %s/%s: %s", args[0], args[1], exc)
                return []

        # Priority order for merge (not start order of awaits).
        specs: List[Tuple[tuple, dict]] = [
            (("process", process_run_id, "working"), {"limit": 8, "query": query}),
            (("process", process_run_id, "episodic"), {"limit": 5, "query": query}),
        ]
        if task_id:
            specs.append(
                (("task", task_id, None), {"limit": 5, "query": query}),
            )
        specs.extend(
            [
                (("procedural", proc_scope, "procedural"), {"limit": 5, "query": query}),
                (("user", user_scope_id, "semantic"), {"limit": 5, "query": query}),
                (("user", user_scope_id, "pinned"), {"limit": 3, "query": query}),
            ]
        )

        sem = asyncio.Semaphore(MEMORY_READ_CONCURRENCY)

        async def _gated(args: tuple, kwargs: dict) -> List[Dict[str, Any]]:
            async with sem:
                return await _safe_read(*args, **kwargs)

        results = await asyncio.gather(
            *[_gated(args, kwargs) for args, kwargs in specs],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Memory parallel read failed: %s", result)
                continue
            _add(result)

        if merged:
            try:
                anchor_id = merged[0].get("id")
                if anchor_id:
                    links = await query_links(anchor_id, direction="both")
                    for link in links[:5]:
                        if link.get("peer_content"):
                            _add(
                                [
                                    {
                                        "id": link.get("peer_id"),
                                        "memory_type": link.get("peer_type") or "graph",
                                        "content": redact_secrets(link["peer_content"]),
                                        "source": "graph",
                                        "confidence": 100,
                                    }
                                ]
                            )
            except Exception as e:
                logger.debug("Graph neighborhood read skipped: %s", e)

        return merged[:limit]

    @staticmethod
    async def compact_episodic_memory(max_age_days: int = 30) -> Dict[str, Any]:
        cutoff = datetime.utcnow() - timedelta(days=max_age_days)
        vector_deleted = 0
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(MemoryItem).where(
                    MemoryItem.memory_type == "episodic",
                    MemoryItem.valid_to.is_(None),
                    MemoryItem.created_at < cutoff,
                )
            )
            items = result.scalars().all()
            count = 0
            if is_vector_memory_enabled() and items:
                from app.memory.vector_sync import delete_memory_points

                point_ids = [item.id for item in items] + [
                    (item.provenance_ref or {}).get("vector_ref") for item in items
                ]
                try:
                    vector_deleted = await asyncio.to_thread(delete_memory_points, point_ids)
                except Exception as exc:
                    logger.warning("Compaction vector delete deferred to reconcile: %s", exc)
            for item in items:
                item.valid_to = datetime.utcnow()
                count += 1
            await db.commit()
        return {
            "compacted": count,
            "vector_deleted": vector_deleted,
            "max_age_days": max_age_days,
        }

    @staticmethod
    async def vector_status() -> Dict[str, Any]:
        cfg = get_vector_memory_config()
        if not is_vector_memory_enabled():
            return {
                "enabled": False,
                "ready": False,
                "embedder_model": cfg.get("embedder_model"),
                "embedding_dims": cfg.get("embedding_dims"),
                "collection": cfg.get("collection_name"),
                "error": cfg.get("not_ready_reason") or "vector memory disabled",
            }
        svc = get_vector_service()
        return await asyncio.to_thread(svc.status)

    @staticmethod
    async def build_context_block(
        process_run_id: str,
        query: Optional[str] = None,
        task_id: Optional[str] = None,
        process_type: str = "generic",
        user_scope_id: str = "default",
        skip_vector: bool = False,
    ) -> str:
        items = await MemoryRouter.read_ordered(
            process_run_id,
            task_id=task_id,
            user_scope_id=user_scope_id,
            process_type=process_type,
            query=query,
            limit=12,
            skip_vector=skip_vector,
        )
        if not items:
            items = await MemoryRouter.read(
                "user", user_scope_id, "pinned", limit=3, query=query, skip_vector=skip_vector
            )
            if not items:
                items = await MemoryRouter.read(
                    "user",
                    user_scope_id,
                    "semantic",
                    limit=5,
                    query=query,
                    skip_vector=skip_vector,
                )
        if not items:
            return (
                "PROCESS-SCOPED MEMORY: (empty — do NOT use workspace memory_search; "
                "use tools only as directed in step instructions)\n"
            )
        lines = [
            "PROCESS-SCOPED MEMORY (use this before workspace files when answering):"
        ]
        for item in items:
            src = item.get("source", "postgres")
            tag = item.get("memory_type", "working")
            if src == "vector":
                tag = f"{tag}/vector"
            elif src == "graph":
                tag = f"{tag}/graph"
            lines.append(f"- [{tag}] {item['content'][:500]}")
        return "\n".join(lines) + "\n"
