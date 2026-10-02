"""Deep-memory ingestion, stage 2: the model's summaries and chunk context headers.

One structured call per section returns the section's summary and a context header for each
of its chunks (contextual retrieval: the header situates the chunk in its document before it
is embedded and indexed for BM25). One call per document then builds its summary from the
section summaries; for a task it also states the outcome and the answer Aura gave. The
changed chunks, the sections and the document are re-indexed, and a task's registry entry
is rebuilt with the summary.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_deep_memory_config
from app.db.database import AsyncSessionLocal
from app.db.models import DeepChunk, DeepDocument, DeepSection, Task, TaskMessage, VectorOutbox
from app.deep_memory.index import point_ref
from app.llm.openai_direct import structured_call
from app.orchestrator.prompt_policy import USER_TIMEZONE

logger = logging.getLogger("rmp.deep_memory.enrich")

CHUNKS_PER_CALL = 24
FINAL_REPLY_CHARS = 6000
SECTION_SUMMARY_CHARS = 1200

KIND_LABELS = {
    "task": "Task record (conversation, route through the system, deliverables, actions, Claude sessions)",
    "deliverable": "Aura's deliverable",
    "tool_document": "Page or file Aura read",
    "claude_session": "Aura's conversation with Claude Code: what she asked, what it did and answered",
    "attachment": "File Kirill sent",
}

SECTION_INSTRUCTIONS = (
    "You are building the long-term memory of Aura, Kirill's personal assistant, for search. "
    "You get one section of a document: the document's kind, title and source, its outline with "
    "this section marked, and the section's chunks, numbered.\n"
    "summary: 2 to 4 sentences with the section's substance: names, numbers, dates, decisions, "
    "requests and answers. No filler and no 'this section'.\n"
    "contexts: for every chunk number, 1 to 3 sentences (50 to 100 tokens) that situate the chunk "
    "in the document so a search finds it: which document and section it comes from, what it is "
    "about, and what its references mean (who 'he' or 'it' is, which task, which day). Do not "
    "repeat the chunk. Write in the language of the text."
)
DOCUMENT_INSTRUCTIONS = (
    "You are building the long-term memory of Aura, Kirill's personal assistant, for search. "
    "You get a document's kind, title and source, and the summaries of its sections in order.\n"
    "summary: 3 to 6 sentences on what the document is and its most useful content: names, "
    "numbers, dates, steps. Write in the language of the text."
)
TASK_INSTRUCTIONS = (
    "You are building the long-term memory of Aura, Kirill's personal assistant, for search. "
    "You get one task: what Kirill asked, the summaries of its sections (conversation, path "
    "through the system, deliverables, actions) and Aura's final reply.\n"
    "summary: 3 to 6 sentences: what Kirill wanted, what Aura did, and how it ended.\n"
    "outcome: one sentence with the result (answered, delivered what, failed why, stopped).\n"
    "answer: the substance of Aura's answer in at most 120 words (the facts, numbers, "
    "recommendations or decisions she gave), or an empty string if she gave none. "
    "Write in the language of the text."
)


class ChunkContext(BaseModel):
    chunk: int
    context: str


class SectionEnrichment(BaseModel):
    summary: str
    contexts: List[ChunkContext]


class DocumentEnrichment(BaseModel):
    summary: str


class TaskEnrichment(BaseModel):
    summary: str
    outcome: str
    answer: str


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def _local(dt: Optional[datetime]) -> str:
    if dt is None:
        return "unknown date"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(USER_TIMEZONE)).strftime("%Y-%m-%d %H:%M JST")


def _source_line(doc: DeepDocument, task: Optional[Task]) -> str:
    ref = doc.source_ref or {}
    where = ref.get("url") or ref.get("path") or ref.get("file_path") or ref.get("name") or ""
    parts = [f"KIND: {KIND_LABELS.get(doc.kind, doc.kind)}", f"TITLE: {doc.title or '(untitled)'}"]
    if where:
        parts.append(f"SOURCE: {where}")
    if ref.get("untrusted"):
        parts.append("TRUST: external web content")
    if task is not None and doc.kind != "task":
        parts.append(f"READ OR WRITTEN DURING THE TASK: {(task.goal or '')[:300]}")
    parts.append(f"DATE: {_local(doc.source_at or doc.created_at)}")
    return "\n".join(parts)


def _section_input(
    doc: DeepDocument, task: Optional[Task], sections: Sequence[DeepSection], current: DeepSection,
    chunks: Sequence[DeepChunk],
) -> str:
    outline = "\n".join(
        f"- {s.path or s.title}{'   <- this section' if s.id == current.id else ''}" for s in sections
    )
    numbered = "\n\n".join(f"[{n}] {c.text}" for n, c in enumerate(chunks))
    return f"{_source_line(doc, task)}\n\nOUTLINE:\n{outline}\n\nSECTION: {current.path or current.title}\n\nCHUNKS:\n{numbered}"


async def _enrich_section(
    doc: DeepDocument, task: Optional[Task], sections: Sequence[DeepSection], section: DeepSection,
    chunks: Sequence[DeepChunk],
) -> Tuple[str, Dict[str, str]]:
    """(section summary, chunk id -> context header), in batches the model can answer whole."""
    summaries: List[str] = []
    headers: Dict[str, str] = {}
    for start in range(0, len(chunks), CHUNKS_PER_CALL):
        batch = list(chunks[start:start + CHUNKS_PER_CALL])
        for attempt in (1, 2):
            result = await structured_call(
                SectionEnrichment,
                purpose="deep_memory.section",
                instructions=SECTION_INSTRUCTIONS,
                input_text=_section_input(doc, task, sections, section, batch),
                priority="enrich",
                max_output_tokens=8000,
            )
            given = {c.chunk: " ".join(c.context.split()) for c in result.value.contexts if c.context.strip()}
            if set(given) == set(range(len(batch))):
                break
            logger.warning("Section %s got contexts for %s of %d chunks (attempt %d)",
                           section.id, sorted(given), len(batch), attempt)
        else:
            raise ValueError(f"section {section.id} got contexts for {sorted(given)} of {len(batch)} chunks")
        headers.update({chunk.id: given[n] for n, chunk in enumerate(batch)})
        summaries.append(" ".join(result.value.summary.split()))
    return " ".join(summaries)[:SECTION_SUMMARY_CHARS], headers


async def _final_reply(read: AsyncSession, task_id: str) -> str:
    """Aura's last answer; rows from before message kinds existed count as replies."""
    reply = (
        await read.execute(
            select(TaskMessage.content)
            .where(TaskMessage.task_id == task_id, TaskMessage.role == "assistant",
                   or_(TaskMessage.kind.in_(("reply", "followup")), TaskMessage.kind.is_(None)))
            .order_by(TaskMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (reply or "")[:FINAL_REPLY_CHARS]


async def enrich_document(db: AsyncSession, document_id: str) -> str:
    """Summaries and chunk headers for one document; model calls run outside any transaction."""
    async with AsyncSessionLocal() as read:
        doc = await read.get(DeepDocument, document_id)
        if doc is None or doc.valid_to is not None:
            return "gone"
        sections = (
            await read.execute(
                select(DeepSection).where(DeepSection.document_id == doc.id).order_by(DeepSection.ordinal)
            )
        ).scalars().all()
        chunks = (
            await read.execute(
                select(DeepChunk).where(DeepChunk.document_id == doc.id, DeepChunk.valid_to.is_(None))
                .order_by(DeepChunk.ordinal)
            )
        ).scalars().all()
        task = await read.get(Task, doc.task_id) if doc.task_id else None
        final_reply = await _final_reply(read, task.id) if task is not None and doc.kind == "task" else ""
    by_section: Dict[str, List[DeepChunk]] = {}
    for chunk in chunks:
        by_section.setdefault(chunk.section_id or "", []).append(chunk)
    stale = [
        s for s in sections
        if by_section.get(s.id) and (not s.summary or any(not c.context_header for c in by_section[s.id]))
    ]
    if not stale and doc.summary:
        return "done"

    limit = asyncio.Semaphore(max(1, int(get_deep_memory_config().get("ingest_concurrency") or 2)))

    async def run(section: DeepSection):
        async with limit:
            return section.id, await _enrich_section(doc, task, sections, section, by_section[section.id])

    enriched = dict(await asyncio.gather(*(run(s) for s in stale)))
    summaries = {s.id: enriched[s.id][0] if s.id in enriched else (s.summary or "") for s in sections}
    ordered = [(s, summaries[s.id]) for s in sections if summaries[s.id]]
    meta_update: Dict[str, Any] = {}
    if doc.kind == "task":
        body = "\n".join(f"- {s.path or s.title}: {text}" for s, text in ordered)
        result = await structured_call(
            TaskEnrichment,
            purpose="deep_memory.task",
            instructions=TASK_INSTRUCTIONS,
            input_text=(
                f"{_source_line(doc, task)}\n\nKIRILL ASKED:\n{(task.goal if task else doc.title) or ''}\n\n"
                f"SECTIONS:\n{body}\n\nAURA'S FINAL REPLY:\n{final_reply or '(none)'}"
            ),
            priority="enrich",
            max_output_tokens=4000,
        )
        task_value = result.value
        meta_update = {"outcome": " ".join(task_value.outcome.split()), "answer": " ".join(task_value.answer.split())}
        summary = " ".join(task_value.summary.split())
        if meta_update["outcome"]:
            summary += f" Outcome: {meta_update['outcome']}"
        if meta_update["answer"]:
            summary += f" Answer: {meta_update['answer']}"
    elif len(ordered) == 1:
        summary = ordered[0][1]
    else:
        body = "\n".join(f"- {s.path or s.title}: {text}" for s, text in ordered)
        result = await structured_call(
            DocumentEnrichment,
            purpose="deep_memory.document",
            instructions=DOCUMENT_INSTRUCTIONS,
            input_text=f"{_source_line(doc, task)}\n\nSECTIONS:\n{body}",
            priority="enrich",
            max_output_tokens=3000,
        )
        summary = " ".join(result.value.summary.split())

    snapshot = {c.id: _digest(c.text) for c in chunks}
    live_doc = await db.get(DeepDocument, document_id)
    if live_doc is None or live_doc.valid_to is not None:
        return "gone"
    refs: List[str] = []
    for section_id, (section_summary, headers) in enriched.items():
        section = await db.get(DeepSection, section_id)
        if section is None:
            continue
        section.summary = section_summary
        refs.append(point_ref("section", section_id))
        for chunk_id, header in headers.items():
            chunk = await db.get(DeepChunk, chunk_id)
            # A chunk rewritten since it was read keeps no header; its next enrichment writes one.
            if chunk is not None and _digest(chunk.text) == snapshot.get(chunk_id):
                chunk.context_header = header
                refs.append(point_ref("chunk", chunk_id))
    live_doc.summary = summary
    live_doc.meta = {**(live_doc.meta or {}), **meta_update}
    live_doc.toc = [{**entry, "summary": summaries.get(entry.get("section_id"), "")} for entry in (live_doc.toc or [])]
    live_doc.status = "enriched"
    live_doc.enriched_at = datetime.utcnow()
    refs.append(point_ref("document", document_id))
    for ref in dict.fromkeys(refs):
        db.add(VectorOutbox(kind="deep", ref_id=ref))
    if live_doc.kind == "task" and live_doc.task_id:
        db.add(VectorOutbox(kind="registry", ref_id=live_doc.task_id))
    await db.flush()
    return "done"
