"""The Internal Agent's fast context for Aura: one memory block, assembled once, in seconds.

In priority order: the recent dialogue of this Slack conversation; the facts about Kirill that
matter for the request (above a dense-similarity floor, so words in common are not enough, and
never a superseded fact); the earlier tasks intake linked to this one; what this run has done so
far; and up to three procedures that worked on similar tasks. Each section has a share of a
fixed budget, and a section that misses the deadline is left out. The deep recall adds to this
while Aura works.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Dict, List, Optional

from sqlalchemy import select

from app.config import get_deep_memory_config
from app.db.database import AsyncSessionLocal
from app.db.models import DeepDocument, Event, MemoryItem, Task, TaskIntakeDecision, TaskRegistryEntry
from app.deep_memory import index
from app.deep_memory.ingest import document_id, ingestible, task_source_key
from app.memory.policy import redact_secrets

logger = logging.getLogger("rmp.deep_memory.curator")

HEADER = "PROCESS-SCOPED MEMORY (use this before workspace files when answering):"
EMPTY = (
    "PROCESS-SCOPED MEMORY: (empty — do NOT use workspace memory_search; "
    "use tools only as directed in step instructions)\n"
)
ORDER = ("dialogue", "facts", "linked", "run", "procedures")
CAPS = {"dialogue": 2600, "facts": 1400, "linked": 1200, "run": 1200, "procedures": 600}
MIN_SECTION_CHARS = 200
USER_SCOPE = "default"
LINKED_TASKS = 3
# Procedures live in the legacy index (text-embedding-3-small), where related tasks score above this.
PROCEDURE_FLOOR = 0.35


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fit(block: str, cap: int, *, keep_end: bool = False) -> str:
    """The block cut to cap on line boundaries; the dialogue keeps its newest lines."""
    if len(block) <= cap:
        return block
    lines = block.split("\n")
    head, body = lines[0], lines[1:]
    if keep_end:
        kept: List[str] = []
        for line in reversed(body):
            if len(head) + sum(len(k) + 1 for k in kept) + len(line) + 1 > cap:
                break
            kept.insert(0, line)
        return "\n".join([head, *kept]) if kept else ""
    out = [head]
    for line in body:
        if sum(len(o) + 1 for o in out) + len(line) > cap:
            break
        out.append(line)
    return "\n".join(out) if len(out) > 1 else ""


async def _dialogue(task: Task) -> str:
    from app.task_registry.messages import recent_session_dialogue_block

    return await recent_session_dialogue_block(task.openclaw_session_key or "", exclude_task_id=task.id)


async def _facts(query: str, floor: float) -> str:
    hits: Optional[List[index.Hit]] = None
    if index.is_enabled():
        try:
            if await asyncio.to_thread(index.collection_exists):
                hits = await asyncio.to_thread(
                    index.search, query, levels=("fact",), match={"scope_id": USER_SCOPE}, limit=8,
                    dense_floor=floor,
                )
        except Exception as exc:
            logger.warning("Fast-context fact search fell back to text search: %s", exc)
    if hits is None:
        hits = (await index.fts_search(query, levels=("fact",), match={"scope_id": USER_SCOPE}, limit=3)) or []
    async with AsyncSessionLocal() as db:
        pinned = (
            await db.execute(
                select(MemoryItem).where(
                    MemoryItem.scope_type == "user", MemoryItem.scope_id == USER_SCOPE,
                    MemoryItem.memory_type == "pinned", MemoryItem.valid_to.is_(None),
                ).order_by(MemoryItem.created_at.desc()).limit(3)
            )
        ).scalars().all()
    lines: List[str] = []
    seen: set = set()
    for text, when in [(p.content, index.iso_utc(p.valid_from or p.created_at)) for p in pinned] + [
        (h.payload.get("text"), h.payload.get("source_at")) for h in hits
    ]:
        body = _clip(redact_secrets(text or ""), 400)
        if not body or body in seen:
            continue
        seen.add(body)
        lines.append(f"- [{(when or '')[:10] or 'undated'}] {body}")
    if not lines:
        return ""
    return "FACTS FROM EARLIER CONVERSATIONS (current ones only; the newest wins):\n" + "\n".join(lines)


async def _linked(task: Task) -> str:
    if not task.intake_decision_id:
        return ""
    async with AsyncSessionLocal() as db:
        decision = await db.get(TaskIntakeDecision, task.intake_decision_id)
        related = [
            t for t in (decision.similar_task_ids or []) if isinstance(t, str) and t != task.id
        ][:LINKED_TASKS] if decision is not None else []
        lines: List[str] = []
        for other_id in related:
            other = await db.get(Task, other_id)
            if other is None or not ingestible(other):
                continue
            record = await db.get(DeepDocument, document_id(task_source_key(other_id)))
            if record is not None and record.summary:
                gist = record.summary
            else:
                entry = (
                    await db.execute(select(TaskRegistryEntry).where(TaskRegistryEntry.task_id == other_id))
                ).scalar_one_or_none()
                gist = entry.outcome_summary if entry is not None else ""
            when = index.iso_utc(other.created_at) or ""
            lines.append(
                f"- {other_id[:8]} ({when[:10]}, {other.status}): “{_clip(other.goal or '', 120)}” — {_clip(gist, 380)}"
            )
    if not lines:
        return ""
    return "EARLIER TASKS THIS ONE RELATES TO (from intake):\n" + "\n".join(lines)


async def _run_memory(process_run_id: str) -> str:
    from app.memory.router import MemoryRouter

    if not process_run_id:
        return ""
    working = await MemoryRouter.read("process", process_run_id, "working", limit=8, skip_vector=True)
    episodic = await MemoryRouter.read("process", process_run_id, "episodic", limit=5, skip_vector=True)
    lines = [f"- [{m['memory_type']}] {_clip(m['content'], 300)}" for m in working + episodic if m.get("content")]
    if not lines:
        return ""
    return "THIS TASK SO FAR:\n" + "\n".join(lines)


async def _procedures(process_type: str, query: str) -> str:
    from app.memory.router import MemoryRouter

    hits = await MemoryRouter.search_semantic("procedural", process_type or "generic", query, limit=3)
    lines = [
        f"- {_clip(h['content'], 400)}" for h in hits
        if h.get("content") and (h.get("source") != "vector" or float(h.get("score") or 0) >= PROCEDURE_FLOOR)
    ]
    if not lines:
        return ""
    return "PROCEDURES THAT WORKED ON SIMILAR TASKS:\n" + "\n".join(lines)


async def warm_up() -> None:
    """Open the index, embedder and legacy memory clients before the first task needs them."""
    from app.memory.vector import get_vector_service

    started = time.monotonic()
    try:
        if index.is_enabled() and await asyncio.to_thread(index.collection_exists):
            await asyncio.to_thread(index.embed_query, "warm up")
        await asyncio.to_thread(get_vector_service().status)
    except Exception as exc:
        logger.warning("Fast-context warm-up incomplete: %s", exc)
        return
    logger.info("Fast-context clients warm in %.1fs", time.monotonic() - started)


async def assemble_fast_context(
    *,
    task_id: Optional[str],
    process_run_id: str,
    process_type: str,
    query: Optional[str],
    skip_vector: bool = False,
) -> str:
    cfg = get_deep_memory_config()
    started = time.monotonic()
    deadline = started + float(cfg.get("fast_context_deadline_sec") or 3.0)
    budget = int(cfg.get("fast_context_max_chars") or 6000)
    floor = float(cfg.get("fast_context_fact_floor") or 0.30)
    task = None
    if task_id:
        async with AsyncSessionLocal() as db:
            task = await db.get(Task, task_id)
    user_work = ingestible(task)
    search = bool(query) and not skip_vector and user_work
    legs: Dict[str, Awaitable[str]] = {"run": _run_memory(process_run_id)}
    if user_work:
        legs["dialogue"] = _dialogue(task)
        legs["linked"] = _linked(task)
    if search:
        legs["facts"] = _facts(query, floor)
        legs["procedures"] = _procedures(process_type, query)

    async def bounded(name: str, leg: Awaitable[str]) -> str:
        try:
            return await asyncio.wait_for(leg, timeout=max(0.05, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            logger.warning("Fast context left out %s: over the deadline", name)
        except Exception as exc:
            logger.warning("Fast context left out %s: %s", name, exc)
        return ""

    names = list(legs)
    results = dict(zip(names, await asyncio.gather(*(bounded(n, legs[n]) for n in names))))
    parts, used, sections = [HEADER], len(HEADER), []
    for name in ORDER:
        text = (results.get(name) or "").strip()
        if not text:
            continue
        room = min(CAPS[name], budget - used - 2)
        if room < MIN_SECTION_CHARS:
            break
        text = _fit(text, room, keep_end=name == "dialogue")
        if not text:
            continue
        parts.append(text)
        used += len(text) + 2
        sections.append(name)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if task_id and task is not None:
        try:
            async with AsyncSessionLocal() as db:
                db.add(Event(correlation_id=task_id, entity_type="task", entity_id=task_id,
                             event_type="memory.fast_context",
                             event_payload={"ms": elapsed_ms, "sections": sections, "chars": used}))
                await db.commit()
        except Exception as exc:
            logger.debug("Fast-context event not recorded: %s", exc)
    if not sections:
        return EMPTY
    return "\n\n".join(parts) + "\n"
