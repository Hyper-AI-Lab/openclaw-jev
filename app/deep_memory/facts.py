"""Atomic facts from conversations: extracted, consolidated, dated, and retired when they change.

Once a task's reply has reached Kirill, the IA reads the task's turns and extracts the durable
facts they state (his details, preferences, plans, decisions, commitments, projects,
relationships, events, and what Aura established for him), each citing the turns it came from.
Every candidate is compared with the nearest active facts and judged the same, an update, a
contradiction or new. An update retires the old fact (``valid_to``) and links the new one to it
(``supersedes``); a contradiction keeps both and links them; Jev reviews what is written in its
promotion mode. Credentials are never stored.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.models import MemoryItem, MemoryLink, Task, TaskMessage, VectorOutbox
from app.deep_memory import index
from app.deep_memory.ingest import TURN_KINDS, _turn_kind, document_id, ingestible, stable_id, task_source_key
from app.llm.openai_direct import structured_call
from app.memory.policy import redact_secrets
from app.orchestrator.prompt_policy import USER_TIMEZONE

logger = logging.getLogger("rmp.deep_memory.facts")

USER_SCOPE = "default"
TURN_CHARS = 3000
NEIGHBOURS = 5
MIN_CONFIDENCE = 0.6
# Natural-language credentials the redaction patterns do not see; a fact naming one is dropped.
_CREDENTIAL = re.compile(
    r"\b(password|passcode|passphrase|api[ _-]?key|access[ _-]?key|secret[ _-]?key|private[ _-]?key|"
    r"auth(entication)?[ _-]?token|access[ _-]?token|bearer|otp|one[- ]time (code|password)|2fa code|"
    r"cvv|cvc|iban|card number|account number|seed phrase|recovery (code|phrase))\b",
    re.IGNORECASE,
)
_PIN = re.compile(r"\bPIN\b")

FactKind = Literal[
    "personal", "preference", "plan", "decision", "commitment", "project", "relationship", "event",
    "health", "professional", "other",
]

EXTRACT_INSTRUCTIONS = (
    "You maintain the long-term memory of Aura, Kirill's personal assistant. From the numbered turns "
    "of one conversation, extract the facts worth remembering across future conversations:\n"
    "- about Kirill: personal details, names and relationships, preferences and dislikes, plans and "
    "intentions, commitments, decisions, projects, work, health, important dates and events, and "
    "codes or labels he asks Aura to remember;\n"
    "- what Aura established for him that he will build on: recommendations he accepted, decisions "
    "agreed, results of work done for him;\n"
    "- key facts from content he shared.\n"
    "Each fact: statement, one standalone sentence in the third person with names instead of "
    "pronouns (\"Kirill's test code word is PELICAN-47.\"), dated when the date matters; subject, a "
    "short name for what it is about (\"test code word\"); kind; said_by; turns, the numbers of the "
    "turns it comes from; valid_from, the date it became true if the text gives one (YYYY-MM-DD), "
    "else null; confidence from 0 to 1.\n"
    "Do not extract greetings, questions, one-off requests, the assistant's working steps, anything "
    "about the assistant system itself, or anything uncertain. Never extract passwords, API keys, "
    "tokens, card or account numbers, or other credentials. Return an empty list when nothing qualifies."
)
CONSOLIDATE_INSTRUCTIONS = (
    "You maintain the long-term memory of Aura, Kirill's personal assistant. For every new candidate "
    "fact, decide against the existing facts listed for it:\n"
    "- same: an existing fact already says this (existing: its number).\n"
    "- update: it replaces an existing fact about the same thing with a newer value, for example "
    "Kirill changed his code word or moved city (existing: the fact it replaces; statement: the new "
    "fact, saying what it replaces and when, e.g. \"Kirill's test code word is HERON-12 (changed from "
    "PELICAN-47 on 2026-09-30).\").\n"
    "- contradicts: it conflicts with an existing fact without clearly replacing it, for example two "
    "sources disagree (existing: that fact; statement: the candidate).\n"
    "- new: nothing listed covers it (existing: null; statement: the candidate).\n"
    "Kirill's own statements about himself replace older ones. Keep statements standalone."
)


class FactCandidate(BaseModel):
    statement: str
    subject: str
    kind: FactKind
    said_by: Literal["kirill", "aura"]
    turns: List[int]
    valid_from: Optional[str]
    confidence: float


class FactExtraction(BaseModel):
    facts: List[FactCandidate]


class FactDecision(BaseModel):
    candidate: int
    action: Literal["new", "same", "update", "contradicts"]
    existing: Optional[int]
    statement: str


class Consolidation(BaseModel):
    decisions: List[FactDecision]


def is_credential(statement: str) -> bool:
    return (
        redact_secrets(statement) != statement
        or bool(_CREDENTIAL.search(statement))
        or bool(_PIN.search(statement))
    )


def _local(dt: Optional[datetime]) -> str:
    if dt is None:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(USER_TIMEZONE)).strftime("%Y-%m-%d %H:%M JST")


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime((value or "").strip()[:10], "%Y-%m-%d") if value else None
    except ValueError:
        return None


def _turn_chunk_id(task_id: str, message_id: str) -> str:
    """The first chunk stage 1 writes for a turn, so a fact can cite it before or after ingestion."""
    return stable_id("chunk", document_id(task_source_key(task_id)), f"turn:{message_id}", "0")


async def _neighbours(statement: str) -> List[str]:
    """Ids of the nearest active user facts, from the hybrid index or else Postgres text search."""
    try:
        if await asyncio.to_thread(index.collection_exists):
            hits = await asyncio.to_thread(
                index.search, statement, levels=("fact",), match={"scope_id": USER_SCOPE}, limit=NEIGHBOURS
            )
            return [h.payload.get("ref_id") or h.id for h in hits]
    except Exception as exc:
        logger.warning("Fact neighbour search fell back to text search: %s", exc)
    hits = await index.fts_search(statement, levels=("fact",), match={"scope_id": USER_SCOPE}, limit=NEIGHBOURS)
    return [h.id for h in hits or []]


async def extract_task_facts(db: AsyncSession, task_id: str) -> str:
    """Stage 3 for one task: facts from its turns, consolidated into user memory."""
    async with AsyncSessionLocal() as read:
        task = await read.get(Task, task_id)
        if not ingestible(task):
            return "internal"
        messages = (
            await read.execute(
                select(TaskMessage).where(TaskMessage.task_id == task_id).order_by(TaskMessage.created_at)
            )
        ).scalars().all()
    turns = [m for m in messages if _turn_kind(m) in TURN_KINDS and m.role in ("user", "assistant")]
    if not any(m.role == "assistant" for m in turns):
        return "no delivered reply"
    listing = "\n\n".join(
        f"[{n}] {'Kirill' if m.role == 'user' else 'Aura'} ({_local(m.created_at)}): {(m.content or '')[:TURN_CHARS]}"
        for n, m in enumerate(turns)
    )
    extraction = await structured_call(
        FactExtraction,
        purpose="deep_memory.facts",
        instructions=EXTRACT_INSTRUCTIONS,
        input_text=f"TASK: {(task.goal or '')[:500]}\n\nTURNS:\n{listing}",
        priority="enrich",
        max_output_tokens=6000,
    )
    candidates: List[FactCandidate] = []
    for fact in extraction.value.facts:
        statement = " ".join(fact.statement.split())
        if not statement or fact.confidence < MIN_CONFIDENCE:
            continue
        if is_credential(statement):
            logger.info("Credential-like fact dropped for task %s", task_id[:8])
            continue
        fact.turns = [n for n in fact.turns if 0 <= n < len(turns)]
        fact.statement = statement
        candidates.append(fact)
    if not candidates:
        return "no facts"

    near: List[List[str]] = [await _neighbours(c.statement) for c in candidates]
    existing_ids = list(dict.fromkeys(i for ids in near for i in ids))
    existing: Dict[str, MemoryItem] = {}
    if existing_ids:
        async with AsyncSessionLocal() as read:
            rows = (
                await read.execute(
                    select(MemoryItem).where(
                        MemoryItem.id.in_(existing_ids), MemoryItem.valid_to.is_(None),
                        MemoryItem.scope_type == "user",
                    )
                )
            ).scalars().all()
        existing = {r.id: r for r in rows}
    numbered = [i for i in existing_ids if i in existing]
    decisions = await _consolidate(candidates, near, numbered, existing)

    from app.decisions.memory import review_promotions

    to_write = [(n, d) for n, d in decisions if d.action != "same"]
    review = await review_promotions(
        listing, [{"content": d.statement, "kind": candidates[n].kind} for n, d in to_write],
        scope_key=f"{USER_SCOPE}:facts:{task_id}",
    )
    allowed = {to_write[i][0] for i in review["allowed_indices"]}
    written = await _apply(db, task, turns, candidates, decisions, numbered, existing, allowed)
    logger.info("Facts for task %s: %d candidates, %d written, jev=%s", task_id[:8], len(candidates), written,
                review["mode"])
    return "done"


async def _consolidate(
    candidates: Sequence[FactCandidate], near: Sequence[Sequence[str]], numbered: Sequence[str],
    existing: Dict[str, MemoryItem],
) -> List[Tuple[int, FactDecision]]:
    """(candidate number, decision) for every candidate; one call when anything is near."""
    if not numbered:
        return [(n, FactDecision(candidate=n, action="new", existing=None, statement=c.statement))
                for n, c in enumerate(candidates)]
    number = {fact_id: e for e, fact_id in enumerate(numbered)}
    lines = ["NEW CANDIDATES:"]
    for n, c in enumerate(candidates):
        close = [f"E{number[i]}" for i in near[n] if i in number]
        lines.append(f"[C{n}] ({c.said_by}) {c.statement}  subject: {c.subject}; nearest existing: "
                     f"{', '.join(close) or 'none'}")
    lines.append("\nEXISTING FACTS:")
    for e, fact_id in enumerate(numbered):
        item = existing[fact_id]
        subject = (item.provenance_ref or {}).get("subject") or ""
        lines.append(f"[E{e}] (since {_local(item.valid_from or item.created_at)[:10]}) {item.content}"
                     + (f"  subject: {subject}" if subject else ""))
    result = await structured_call(
        Consolidation,
        purpose="deep_memory.consolidate",
        instructions=CONSOLIDATE_INSTRUCTIONS,
        input_text="\n".join(lines),
        priority="enrich",
        max_output_tokens=4000,
    )
    by_candidate = {d.candidate: d for d in result.value.decisions if 0 <= d.candidate < len(candidates)}
    decisions: List[Tuple[int, FactDecision]] = []
    for n, c in enumerate(candidates):
        d = by_candidate.get(n)
        if d is None or (d.action != "new" and (d.existing is None or not 0 <= d.existing < len(numbered))):
            d = FactDecision(candidate=n, action="new", existing=None, statement=c.statement)
        d.statement = " ".join((d.statement or c.statement).split())
        if is_credential(d.statement):
            continue
        decisions.append((n, d))
    return decisions


async def _apply(
    db: AsyncSession, task: Task, turns: Sequence[TaskMessage], candidates: Sequence[FactCandidate],
    decisions: Sequence[Tuple[int, FactDecision]], numbered: Sequence[str], existing: Dict[str, MemoryItem],
    allowed: set,
) -> int:
    from app.memory.router import MemoryRouter

    written = 0
    now = datetime.utcnow()
    for n, decision in decisions:
        if decision.action == "same" or n not in allowed:
            continue
        candidate = candidates[n]
        sources = [turns[t] for t in candidate.turns] or list(turns[:1])
        target = numbered[decision.existing] if decision.existing is not None and decision.action != "new" else None
        old = await db.get(MemoryItem, target) if target else None
        if target and (old is None or old.valid_to is not None):
            old, target = None, None
        said_at = sources[-1].created_at if sources else now
        fact_id = await MemoryRouter.write(
            "user",
            USER_SCOPE,
            "semantic",
            decision.statement,
            provenance={
                "task_id": task.id,
                "message_ids": [m.id for m in sources],
                "chunk_ids": [_turn_chunk_id(task.id, m.id) for m in sources],
                "subject": candidate.subject[:120],
                "kind": candidate.kind,
                "said_by": candidate.said_by,
                "extracted_by": "ia",
                "consolidation": decision.action,
                **({"supersedes": target} if target and decision.action == "update" else {}),
            },
            confidence=max(1, min(100, int(round(candidate.confidence * 100)))),
            db=db,
            valid_from=_parse_date(candidate.valid_from) or said_at,
            supersedes_memory_id=target if decision.action == "update" else None,
        )
        if old is not None and decision.action == "update":
            old.valid_to = now
            db.add(VectorOutbox(kind="deep", ref_id=index.point_ref("fact", old.id)))
            db.add(MemoryLink(source_id=fact_id, target_id=old.id, relation="supersedes"))
        elif old is not None and decision.action == "contradicts":
            db.add(MemoryLink(source_id=fact_id, target_id=old.id, relation="contradicts"))
        written += 1
    await db.flush()
    return written
