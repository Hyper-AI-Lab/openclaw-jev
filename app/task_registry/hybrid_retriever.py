"""Hybrid retrieval: Postgres FTS + Qdrant dense + memory, fused with RRF.

Retrieval is evidence for the Intake Analyst. It must not assign workflows.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.notification_policy import is_internal_task

logger = logging.getLogger("rmp.hybrid_retriever")

RRF_K = 60
ACTIVE_STATUSES = frozenset(
    {"created", "running", "pending", "pending_user_input", "blocked", "needs_replan"}
)
_FTS_WORD = re.compile(r"[^\W_]{3,}", re.UNICODE)
MAX_FTS_WORDS = 16
FTS_DAYS = 90
FTS_DEADLINE_SEC = 3.0
MEMORY_DEADLINE_SEC = 4.0


def reciprocal_rank_fusion(
    ranked_lists: Iterable[List[str]],
    *,
    k: int = RRF_K,
) -> Dict[str, float]:
    """Merge rank lists with RRF: score = sum 1/(k+rank). Keys are citation ids."""
    scores: Dict[str, float] = {}
    for lst in ranked_lists:
        for rank, doc_id in enumerate(lst, start=1):
            if not doc_id:
                continue
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def status_boost(status: Optional[str], *, age_days: float = 0.0) -> float:
    st = (status or "").lower()
    if st in {"running", "created", "pending"}:
        return 1.35
    if st in {"blocked", "needs_replan", "pending_user_input"}:
        return 1.25
    if st in {"completed"} and age_days <= 7:
        return 1.10
    if st in {"failed", "compensated", "cancelled"}:
        return 0.95
    return 1.0


def or_query(raw: str) -> str:
    """Any of the message's words, for websearch_to_tsquery: a message rarely repeats every word of a task."""
    words = list(dict.fromkeys(w.lower() for w in _FTS_WORD.findall(raw or "")))[:MAX_FTS_WORDS]
    return " or ".join(words)


def _citation_id(*, kind: str, raw_id: str) -> str:
    return f"{kind}:{raw_id}"


async def search_fts(
    query: str,
    *,
    limit: int = 8,
    db: Optional[AsyncSession] = None,
) -> List[Dict[str, Any]]:
    """Lexical hits over registry, task goals, and recent messages: any word, user work, the last 90 days."""
    q = or_query(query)
    if not q:
        return []

    sql_registry = text(
        """
        SELECT task_id, intent_snippet, outcome_summary, terminal_status,
               process_type, session_key,
               ts_rank(
                 to_tsvector('english',
                   coalesce(intent_snippet,'') || ' ' || coalesce(outcome_summary,'')),
                 q.query
               ) AS rank
        FROM task_registry_entries,
             websearch_to_tsquery('english', :q) AS q(query)
        WHERE to_tsvector('english',
                coalesce(intent_snippet,'') || ' ' || coalesce(outcome_summary,''))
              @@ q.query
          AND coalesce(process_type, '') NOT IN ('canary', 'heartbeat')
          AND coalesce(task_ended_at, indexed_at) >= :since
        ORDER BY rank DESC
        LIMIT :lim
        """
    )
    sql_tasks = text(
        """
        SELECT id AS task_id, goal, status, task_type, openclaw_session_key AS session_key,
               ts_rank(to_tsvector('english', coalesce(goal,'')), q.query) AS rank
        FROM tasks, websearch_to_tsquery('english', :q) AS q(query)
        WHERE to_tsvector('english', coalesce(goal,'')) @@ q.query
          AND status IN ('created','running','pending','pending_user_input','blocked','needs_replan')
          AND coalesce(task_type, '') NOT IN ('canary', 'heartbeat')
          AND updated_at >= :since
          -- An intake reservation is the incoming request itself, not existing work.
          AND coalesce(supplementary_context->>'intake_reserved', 'false') <> 'true'
        ORDER BY rank DESC
        LIMIT :lim
        """
    )
    sql_msgs = text(
        """
        SELECT m.task_id, left(m.content, 400) AS snippet, t.status, t.task_type,
               ts_rank(to_tsvector('english', coalesce(m.content,'')), q.query) AS rank
        FROM task_messages m
        JOIN tasks t ON t.id = m.task_id,
             websearch_to_tsquery('english', :q) AS q(query)
        WHERE to_tsvector('english', coalesce(m.content,'')) @@ q.query
          AND coalesce(t.task_type, '') NOT IN ('canary', 'heartbeat')
          AND m.created_at >= :since
        ORDER BY rank DESC
        LIMIT :lim
        """
    )
    since = datetime.utcnow() - timedelta(days=FTS_DAYS)

    async def _run_one(session: AsyncSession, stmt, source: str) -> List[Dict[str, Any]]:
        rows_out: List[Dict[str, Any]] = []
        result = await session.execute(stmt, {"q": q, "lim": limit, "since": since})
        for row in result.mappings().all():
            tid = str(row.get("task_id") or "")
            if not tid:
                continue
            snippet = (
                row.get("intent_snippet")
                or row.get("goal")
                or row.get("snippet")
                or ""
            )
            if is_internal_task(str(snippet), str(row.get("process_type") or row.get("task_type") or ""), []):
                continue
            rows_out.append(
                {
                    "citation": _citation_id(kind="task", raw_id=tid),
                    "task_id": tid,
                    "source": source,
                    "snippet": str(snippet)[:400],
                    "status": row.get("terminal_status") or row.get("status"),
                    "process_type": row.get("process_type") or row.get("task_type"),
                    "fts_rank": float(row.get("rank") or 0),
                    "outcome_summary": row.get("outcome_summary"),
                }
            )
        return rows_out

    hits: List[Dict[str, Any]] = []
    statements = (
        (sql_registry, "fts_registry"),
        (sql_tasks, "fts_active"),
        (sql_msgs, "fts_message"),
    )
    try:
        if db is not None:
            for stmt, source in statements:
                try:
                    hits.extend(await _run_one(db, stmt, source))
                except Exception as exc:
                    logger.warning("FTS %s failed: %s", source, exc)
            return hits
        for stmt, source in statements:
            try:
                async with AsyncSessionLocal() as session:
                    hits.extend(await _run_one(session, stmt, source))
            except Exception as exc:
                logger.warning("FTS %s failed: %s", source, exc)
        return hits
    except Exception as exc:
        logger.warning("FTS search failed: %s", exc)
        return []


async def search_user_memory(query: str, *, limit: int = 5) -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    try:
        from app.memory.router import MemoryRouter

        items = await MemoryRouter.search_semantic("user", "default", q, limit=limit)
        out: List[Dict[str, Any]] = []
        for item in items or []:
            mid = str(item.get("id") or item.get("memory_id") or "")
            content = str(item.get("content") or item.get("text") or "")[:400]
            if not content:
                continue
            cid = _citation_id(kind="memory", raw_id=mid or content[:24])
            out.append(
                {
                    "citation": cid,
                    "memory_id": mid,
                    "source": "memory",
                    "snippet": content,
                    "status": None,
                    "process_type": None,
                }
            )
        return out
    except Exception as exc:
        logger.warning("Memory search failed: %s", exc)
        return []


async def annotate_liveness(task_ids: List[str], *, max_check: int = 8) -> Dict[str, bool]:
    if not task_ids:
        return {}
    from app.temporal_control import workflow_is_running

    out: Dict[str, bool] = {}

    async def _one(tid: str) -> Tuple[str, bool]:
        try:
            return tid, bool(await asyncio.wait_for(workflow_is_running(tid), timeout=2.0))
        except Exception:
            return tid, False

    checks = [_one(tid) for tid in task_ids[:max_check]]
    pairs = await asyncio.gather(*checks, return_exceptions=True)
    for item in pairs:
        if isinstance(item, Exception):
            continue
        tid, running = item
        out[tid] = running
    return out


def fuse_evidence(
    *,
    active: List[Dict[str, Any]],
    fts: List[Dict[str, Any]],
    dense: List[Dict[str, Any]],
    memory: List[Dict[str, Any]],
    limit: int = 12,
) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    lists: List[List[str]] = []

    def _ingest(rows: List[Dict[str, Any]], source: str) -> List[str]:
        order: List[str] = []
        for row in rows:
            cid = row.get("citation") or _citation_id(
                kind="task", raw_id=str(row.get("task_id") or "")
            )
            if not cid or cid.endswith(":"):
                continue
            existing = by_id.get(cid, {})
            merged = {**existing, **{k: v for k, v in row.items() if v is not None}}
            sources = list(existing.get("sources") or [])
            if source not in sources:
                sources.append(source)
            merged["citation"] = cid
            merged["sources"] = sources
            by_id[cid] = merged
            order.append(cid)
        return order

    lists.append(_ingest(active, "active"))
    lists.append(_ingest(fts, "fts"))
    lists.append(_ingest(dense, "dense"))
    lists.append(_ingest(memory, "memory"))

    rrf = reciprocal_rank_fusion(lists)
    now = datetime.utcnow()
    ranked: List[Dict[str, Any]] = []
    for cid, base in rrf.items():
        row = dict(by_id.get(cid) or {"citation": cid})
        age_days = float(row.get("age_days") or 0)
        ended = row.get("task_ended_at") or row.get("updated_at")
        if ended and not row.get("age_days"):
            try:
                if isinstance(ended, str):
                    dt = datetime.fromisoformat(ended.replace("Z", "+00:00")).replace(
                        tzinfo=None
                    )
                else:
                    dt = ended
                age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
            except Exception:
                age_days = 0.0
        boost = status_boost(row.get("status") or row.get("terminal_status"), age_days=age_days)
        row["rrf_score"] = base * boost
        row["age_days"] = age_days
        ranked.append(row)
    ranked.sort(key=lambda r: float(r.get("rrf_score") or 0), reverse=True)
    return ranked[:limit]


async def _bounded_leg(leg, seconds: float, name: str) -> List[Dict[str, Any]]:
    """One evidence source within its own deadline; a slow or failing source adds nothing."""
    try:
        return await asyncio.wait_for(leg, timeout=seconds)
    except asyncio.TimeoutError:
        logger.warning("Evidence leg %s timed out after %.1fs", name, seconds)
    except Exception as exc:
        logger.warning("Evidence leg %s failed: %s", name, exc)
    return []


async def evidence_legs(
    query: str, *, limit: int, deadline_sec: Optional[float] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Text search and user memory, concurrently, each within its own deadline (at most ``deadline_sec``)."""
    cap = float("inf") if deadline_sec is None else deadline_sec
    fts, memory = await asyncio.gather(
        _bounded_leg(search_fts(query, limit=limit), min(FTS_DEADLINE_SEC, cap), "fts"),
        _bounded_leg(search_user_memory(query, limit=5), min(MEMORY_DEADLINE_SEC, cap), "memory"),
    )
    return fts, memory


async def assemble_evidence_pack(
    query: str,
    *,
    active: Optional[List[Dict[str, Any]]] = None,
    recent: Optional[List[Dict[str, Any]]] = None,
    dense: Optional[List[Dict[str, Any]]] = None,
    legs: Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]] = None,
    limit: int = 12,
    include_liveness: bool = True,
) -> Dict[str, Any]:
    """Fuse provided buckets with FTS + memory (run here unless ``legs`` already holds them)."""
    fts, memory = legs if legs is not None else await evidence_legs(query, limit=limit)

    active_rows = []
    for row in active or []:
        goal = row.get("goal") or row.get("goal_snippet") or ""
        if is_internal_task(goal, row.get("task_type") or "", []):
            continue
        active_rows.append(
            {
                **row,
                "citation": _citation_id(kind="task", raw_id=str(row.get("task_id") or "")),
                "snippet": (goal or "")[:400],
                "source": "active",
                "status": row.get("status"),
            }
        )

    dense_rows = []
    for row in dense or []:
        tid = str(row.get("task_id") or "")
        dense_rows.append(
            {
                **row,
                "citation": _citation_id(kind="task", raw_id=tid),
                "snippet": (row.get("intent_snippet") or "")[:400],
                "source": "dense",
                "status": row.get("terminal_status"),
            }
        )

    ranked = fuse_evidence(
        active=active_rows,
        fts=fts,
        dense=dense_rows,
        memory=memory,
        limit=limit,
    )

    if include_liveness:
        active_ids = [
            str(r.get("task_id"))
            for r in active_rows
            if r.get("task_id")
        ]
        live = await annotate_liveness(active_ids)
        for row in ranked:
            tid = row.get("task_id")
            if tid in live:
                row["workflow_running"] = live[tid]
                if row.get("status") in ACTIVE_STATUSES and not live[tid]:
                    row["stale"] = True

    return {
        "ranked": ranked,
        "fts_hits": fts,
        "memory_hits": memory,
        "active_tasks": active_rows,
        "recent_registry": recent or [],
        "vector_similar": dense or [],
    }
