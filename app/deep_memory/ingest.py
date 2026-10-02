"""Deep-memory ingestion, stage 1: documents, their tables of contents and chunks, no model.

Work is queued in dm_ingest_queue in the transaction that writes its source, and drained
by a loop in the API process:

- ``turn``: one of Kirill's or Aura's messages, searchable within seconds;
- ``task``: a finished task's document (Conversation, Path history, Deliverables, Actions),
  plus the pages and files Aura read while doing it;
- ``attachment``: the plain-text files Kirill sent with a message;
- ``enrich``: stage 2 for one document (``app.deep_memory.enrich``), queued when a task
  ends and when an attachment is read;
- ``facts``: stage 3 for one task (``app.deep_memory.facts``), queued by promotion once
  the task's reply has reached Kirill.

Ids are derived from their sources, so ingesting again overwrites instead of duplicating.
Canary, cron and heartbeat runs never enter.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import OPENCLAW_HOME, get_deep_memory_config
from app.db.database import AsyncSessionLocal
from app.db.models import (
    DeepChunk,
    DeepDocument,
    DeepIngestJob,
    DeepLink,
    DeepSection,
    Event,
    ProcessRun,
    Task,
    TaskIntakeDecision,
    TaskMessage,
    VectorOutbox,
)
from app.deep_memory.chunking import chunk_document, chunk_text, chunk_turn, split_sections
from app.deep_memory.index import is_enabled, point_ref
from app.memory.policy import redact_secrets
from app.notification_policy import is_internal_task
from app.orchestrator.decision_engine import INTAKE_PLACEHOLDER
from app.orchestrator.prompt_policy import USER_TIMEZONE

logger = logging.getLogger("rmp.deep_memory.ingest")

NAMESPACE = uuid.UUID("0c1e5b8e-6d8a-4f5b-9a47-3d2f6b1c9e21")
TURN_KINDS = frozenset({"request", "attached", "clarify_answer", "reply", "followup"})
INTERNAL_TASK_TYPES = frozenset({"canary", "heartbeat", "cron"})
PRIORITY = {"turn": 1, "attachment": 2, "task": 3, "facts": 3, "enrich": 4}
# Tools whose results are text Aura read: pages, crawls, extractions and files.
CONTENT_TOOLS = (
    "web_fetch", "read", "jina_reader", "scrapling", "crawlee_crawl",
    "scrapegraph_extract", "browser_use", "obscura_browse",
)
TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".xml", ".html", ".htm", ".log", ".py", ".js", ".ts", ".sh", ".sql",
})
ATTACHMENT_MAX_BYTES = 2_000_000
# A reply this long, or with two or more headings, is a deliverable with its own table of contents.
DELIVERABLE_MIN_CHARS = 2400
POINTER_EXCERPT_CHARS = 500
DRAIN_INTERVAL_SEC = 10
DRAIN_BATCH = 20
MAX_BACKOFF_SEC = 3600
# A claimed job is hidden from other drainers this long; a crash mid-job frees it afterwards.
CLAIM_LEASE_SEC = 300
TASK_SECTIONS = ("Conversation", "Path history", "Deliverables", "Actions")
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+\S", re.MULTILINE)
# OpenClaw wraps fetched pages in these markers, after a security preamble.
_UNTRUSTED = re.compile(
    r"<<<EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>(.*?)<<<END_EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>", re.DOTALL
)
_WEB_TOOLS = frozenset(CONTENT_TOOLS) - {"read"}


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "\x1f".join(parts)))


def document_id(source_key: str) -> str:
    return stable_id("doc", source_key)


def task_source_key(task_id: str) -> str:
    return f"task:{task_id}"


async def enqueue(db: AsyncSession, kind: str, ref_id: str) -> None:
    """Queue ingestion in the caller's transaction; a pending duplicate is a no-op."""
    now = datetime.utcnow()
    values = {
        "id": str(uuid.uuid4()), "kind": kind, "ref_id": ref_id, "priority": PRIORITY[kind],
        "attempts": 0, "next_attempt_at": now, "created_at": now,
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:
        await db.execute(insert(DeepIngestJob).values(**values))
        return
    await db.execute(dialect_insert(DeepIngestJob).values(**values).on_conflict_do_nothing())


async def enqueue_task(task_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await enqueue(db, "task", task_id)
        await db.commit()


async def enqueue_facts(task_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await enqueue(db, "facts", task_id)
        await db.commit()


def ingestible(task: Optional[Task]) -> bool:
    """User work only: never canaries, cron, heartbeats, internal intents, intake placeholders or intake's
    acknowledgements of a message with nothing new to run."""
    if task is None:
        return False
    if (task.task_type or "").lower() in INTERNAL_TASK_TYPES:
        return False
    if is_internal_task(task.goal or "", task.task_type or "", []):
        return False
    ctx = task.supplementary_context or {}
    return not ctx.get("intake_reserved") and not ctx.get("intake_ack") and ctx.get("closed_reason") != INTAKE_PLACEHOLDER


def _local(dt: Optional[datetime]) -> str:
    if dt is None:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(USER_TIMEZONE)).strftime("%Y-%m-%d %H:%M")


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _turn_kind(message: TaskMessage) -> Optional[str]:
    """The message's kind; rows written before kinds existed are Kirill's requests or Aura's replies."""
    if message.kind:
        return message.kind
    return {"user": "request", "assistant": "reply"}.get(message.role or "")


def _unwrap(text: str) -> str:
    parts = _UNTRUSTED.findall(text or "")
    body = "\n\n".join(parts) if parts else (text or "")
    lines = body.strip().split("\n")
    if lines and lines[0].startswith("Source:"):
        lines = lines[1:]
        if lines and lines[0].strip() == "---":
            lines = lines[1:]
    return "\n".join(lines).strip()


def readable_result(text: str) -> Tuple[str, str]:
    """(title, body) of what a reading tool returned: the page inside its JSON envelope and markers."""
    try:
        envelope = json.loads(text)
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict):
        return "", _unwrap(text)
    title = " ".join(_unwrap(str(envelope.get("title") or "")).split())
    for key in ("text", "content", "markdown", "result"):
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return title, _unwrap(value)
    return title, ""


def _is_deliverable(message: TaskMessage) -> bool:
    content = message.content or ""
    return message.role == "assistant" and (
        len(content) >= DELIVERABLE_MIN_CHARS or len(_HEADING.findall(content)) >= 2
    )


def _attachments(message: TaskMessage) -> List[Dict[str, Any]]:
    slack = (message.meta or {}).get("slack") or {}
    return [a for a in slack.get("attachments") or [] if isinstance(a, dict)]


class _Writer:
    """Documents, sections and chunks for one job, with the index work they need."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.refs: List[str] = []
        self.sections: Dict[str, DeepSection] = {}

    async def document(self, source_key: str, kind: str, **fields: Any) -> DeepDocument:
        doc = await self.db.get(DeepDocument, document_id(source_key))
        if doc is None:
            doc = DeepDocument(id=document_id(source_key), kind=kind, source_key=source_key, status="raw")
            self.db.add(doc)
        for key, value in fields.items():
            if value is not None and getattr(doc, key) != value:
                setattr(doc, key, value)
        await self.db.flush()
        return doc

    async def section(self, doc: DeepDocument, key: str, *, ordinal: int, title: str, path: str,
                      level: int = 1) -> DeepSection:
        section_id = stable_id("section", doc.id, key)
        section = await self.db.get(DeepSection, section_id)
        if section is None:
            section = DeepSection(id=section_id, document_id=doc.id, ordinal=ordinal)
            self.db.add(section)
        section.ordinal, section.title, section.path, section.level = ordinal, title, path, level
        await self.db.flush()
        return section

    async def unit(
        self,
        doc: DeepDocument,
        section: DeepSection,
        unit: str,
        texts: Sequence[str],
        **fields: Any,
    ) -> None:
        """Write one source unit's chunks; drop the unit's chunks it no longer has."""
        chunks = (await self.db.execute(select(DeepChunk).where(DeepChunk.document_id == doc.id))).scalars().all()
        by_id = {c.id: c for c in chunks}
        stale = {c.id: c for c in chunks if (c.meta or {}).get("unit") == unit}
        next_ordinal = max((c.ordinal for c in chunks), default=-1) + 1
        meta = {**(fields.pop("meta", None) or {}), "unit": unit}
        for n, text in enumerate(texts):
            chunk_id = stable_id("chunk", doc.id, unit, str(n))
            stale.pop(chunk_id, None)
            row = by_id.get(chunk_id)
            if row is None:
                row = DeepChunk(id=chunk_id, document_id=doc.id, ordinal=next_ordinal, text=text)
                next_ordinal += 1
                self.db.add(row)
            elif row.text == text and row.section_id == section.id and row.valid_to is None:
                continue
            row.section_id, row.text, row.char_count = section.id, text, len(text)
            row.context_header, row.valid_to = None, None
            row.meta = meta
            for key, value in fields.items():
                setattr(row, key, value)
            self.refs.append(point_ref("chunk", chunk_id))
        for chunk_id, row in stale.items():
            await self.db.delete(row)
            self.refs.append(point_ref("chunk", chunk_id))
        self.sections[section.id] = section
        await self.db.flush()

    async def link(self, source_id: str, target_id: str, relation: str, **meta: Any) -> None:
        if source_id == target_id:
            return
        exists = (
            await self.db.execute(
                select(DeepLink.id).where(
                    DeepLink.source_id == source_id, DeepLink.target_id == target_id, DeepLink.relation == relation
                )
            )
        ).first()
        if exists is None:
            self.db.add(DeepLink(source_type="document", source_id=source_id, target_type="document",
                                 target_id=target_id, relation=relation, meta=meta or None))

    async def finish(self) -> None:
        from sqlalchemy import func

        if self.sections:
            counts = dict(
                (
                    await self.db.execute(
                        select(DeepChunk.section_id, func.count())
                        .where(DeepChunk.section_id.in_(list(self.sections)))
                        .group_by(DeepChunk.section_id)
                    )
                ).all()
            )
            for section_id, section in self.sections.items():
                section.chunk_count = int(counts.get(section_id, 0))
        for ref in dict.fromkeys(self.refs):
            self.db.add(VectorOutbox(kind="deep", ref_id=ref))
        await self.db.flush()


async def _document_from_text(
    w: _Writer,
    *,
    source_key: str,
    kind: str,
    title: str,
    text: str,
    fields: Dict[str, Any],
    chunk_fields: Dict[str, Any],
) -> DeepDocument:
    """A document whose headings become its table of contents."""
    sections, chunks = chunk_document(text, root_title=title)
    doc = await w.document(source_key, kind, title=title[:300], content_hash=hashlib.sha256(text.encode()).hexdigest(),
                           **fields)
    toc = []
    for spec in sections:
        section = await w.section(doc, f"ordinal:{spec.ordinal}", ordinal=spec.ordinal, title=spec.title,
                                  path=spec.path, level=spec.level)
        pieces = [c.text for c in chunks if c.section_ordinal == spec.ordinal]
        await w.unit(doc, section, f"section:{spec.ordinal}", pieces, **chunk_fields)
        toc.append({"section_id": section.id, "ordinal": spec.ordinal, "path": spec.path, "title": spec.title,
                    "level": spec.level})
    doc.toc = toc
    return doc


def _turn_text(message: TaskMessage) -> str:
    who = "Kirill" if message.role == "user" else "Aura"
    label = {
        "attached": " (added while Aura was working)",
        "clarify_answer": " (answering Aura's question)",
        "followup": " (follow-up after recalling more)",
    }.get(_turn_kind(message) or "", "")
    names = [str(a.get("name") or "file") for a in _attachments(message)]
    files = f" [attached: {', '.join(names)}]" if names else ""
    return f"{who}{label}: {message.content}{files}"


async def _task_document(w: _Writer, task: Task) -> DeepDocument:
    return await w.document(
        task_source_key(task.id),
        "task",
        title=_clip((task.goal or "").split("\n")[0], 160) or f"Task {task.id[:8]}",
        task_id=task.id,
        session_key=task.openclaw_session_key,
        source_at=task.created_at,
    )


async def _task_section(w: _Writer, doc: DeepDocument, name: str) -> DeepSection:
    return await w.section(doc, name, ordinal=TASK_SECTIONS.index(name), title=name, path=name)


async def ingest_turn(db: AsyncSession, message_id: str, *, writer: Optional[_Writer] = None) -> str:
    message = await db.get(TaskMessage, message_id)
    if message is None:
        return "gone"
    if _turn_kind(message) not in TURN_KINDS or message.role not in ("user", "assistant"):
        return "not a turn"
    task = await db.get(Task, message.task_id)
    if not ingestible(task):
        return "internal"
    w = writer or _Writer(db)
    doc = await _task_document(w, task)
    section = await _task_section(w, doc, "Conversation")
    meta = {
        "kind": _turn_kind(message),
        "slack_ts": message.slack_ts,
        "intake_decision_id": task.intake_decision_id,
        **{k: v for k, v in (message.meta or {}).items() if k in ("attempt", "process_run_id", "signal_type")},
    }
    fields = {
        "task_id": task.id,
        "session_key": message.session_key or task.openclaw_session_key,
        "process_run_id": (message.meta or {}).get("process_run_id"),
        "message_id": message.id,
        "role": message.role,
        "source_at": message.created_at,
        "meta": {k: v for k, v in meta.items() if v is not None},
    }
    if _is_deliverable(message):
        deliverable = await _deliverable(w, task, message)
        titles = ", ".join(e["title"] for e in (deliverable.toc or [])[:8])
        excerpt = _clip(message.content, POINTER_EXCERPT_CHARS)
        pointer = (
            f"Aura (reply): delivered “{deliverable.title}” ({len(message.content):,} characters; "
            f"sections: {titles}). It opens: {excerpt}"
        )
        await w.unit(doc, section, f"turn:{message.id}", [pointer], **fields)
        await w.link(deliverable.id, doc.id, "part_of")
    else:
        await w.unit(doc, section, f"turn:{message.id}", chunk_turn(_turn_text(message)), **fields)
    if message.role == "user" and _attachments(message):
        await enqueue(db, "attachment", message.id)
    if writer is None:
        await w.finish()
    return "done"


async def _deliverable(w: _Writer, task: Task, message: TaskMessage) -> DeepDocument:
    # The outermost heading names the document, even when it has no text of its own.
    roots = [s.path.split(" > ")[0] for s in split_sections(message.content)]
    heading = next((r for r in roots if r not in ("Content", "Introduction")), "")
    ask = (task.goal or "").split("\n")[0]
    title = _clip(heading or f"Answer: {ask}", 160)
    return await _document_from_text(
        w,
        source_key=f"deliverable:{message.id}",
        kind="deliverable",
        title=title,
        text=message.content,
        fields={"task_id": task.id, "session_key": task.openclaw_session_key, "source_at": message.created_at,
                "source_ref": {"message_id": message.id}},
        chunk_fields={"task_id": task.id, "session_key": task.openclaw_session_key, "message_id": message.id,
                      "role": "assistant", "source_at": message.created_at,
                      "meta": {"kind": "deliverable", "intake_decision_id": task.intake_decision_id}},
    )


async def _path_history(db: AsyncSession, task: Task) -> Tuple[str, List[Tuple[str, str]]]:
    """The task's route through RMP, and (related task id, relation) pairs from its intake."""
    lines = [f"Task {task.id[:8]} ({task.task_type or 'user'}): {_clip(task.goal or '', 400)}"]
    ctx = task.supplementary_context or {}
    ended = task.updated_at if task.status in ("completed", "failed", "stopped_by_user", "cancelled", "compensated") else None
    status = f"Status: {task.status}"
    if ctx.get("closed_reason"):
        status += f" ({ctx['closed_reason']})"
    status += f"; asked {_local(task.created_at)} JST"
    if ended:
        minutes = max(0, int((ended - task.created_at).total_seconds() // 60)) if task.created_at else 0
        status += f", ended {_local(ended)} JST after {minutes} min"
    lines.append(status + ".")
    related: List[Tuple[str, str]] = []
    if task.intake_decision_id:
        decision = await db.get(TaskIntakeDecision, task.intake_decision_id)
        if decision is not None:
            relation = "continues" if decision.decision == "rebuild_stale" else "related"
            similar = [s for s in (decision.similar_task_ids or []) if isinstance(s, str) and s != task.id]
            related = [(s, relation) for s in similar[:5]]
            text = f"Intake decided {decision.decision} (confidence {decision.confidence})"
            if decision.rationale:
                text += f": {_clip(decision.rationale, 400)}"
            if similar:
                goals = []
                for other_id in similar[:5]:
                    other = await db.get(Task, other_id)
                    goals.append(f"{other_id[:8]} “{_clip(other.goal or '', 80)}”" if other else other_id[:8])
                text += f". Related tasks: {'; '.join(goals)}"
            lines.append(text + ".")
    runs = (await db.execute(select(ProcessRun).where(ProcessRun.task_id == task.id).order_by(ProcessRun.started_at))).scalars().all()
    for run in runs:
        steps = ((run.plan_json or {}).get("steps") if isinstance(run.plan_json, dict) else None) or []
        names = [f"{s.get('name')} ({s.get('kind')})" for s in steps if isinstance(s, dict)]
        if names:
            lines.append(f"Plan ({run.process_type}): {', '.join(names)}; final state {run.current_state}.")
    messages = (
        await db.execute(select(TaskMessage).where(TaskMessage.task_id == task.id).order_by(TaskMessage.created_at))
    ).scalars().all()
    verdicts = [m for m in messages if m.kind == "verdict" or m.role == "evaluator"]
    for n, verdict in enumerate(verdicts, 1):
        meta = verdict.meta or {}
        lines.append(
            f"Evaluator on attempt {meta.get('attempt') or n}: {meta.get('verdict') or 'verdict'}"
            f" — {_clip(verdict.content, 300)}"
        )
    attached = sum(1 for m in messages if m.kind in ("attached", "clarify_answer"))
    if attached:
        lines.append(f"{attached} message(s) from Kirill joined the task while it ran.")
    events = (
        await db.execute(select(Event.event_type).where(Event.entity_id == task.id).order_by(Event.occurred_at))
    ).scalars().all()
    delivered = sum(1 for e in events if e == "slack.delivered")
    if delivered:
        lines.append(f"Slack: {delivered} message(s) delivered.")
    for marker, text in (
        ("evaluator.escalate", "The evaluator escalated the task to Kirill."),
        ("evaluator.unavailable", "The evaluator stayed unavailable."),
        ("slack.delivery_failed", "Slack refused a reply."),
        ("task.compensated", "The task was compensated after an error."),
    ):
        if marker in events:
            lines.append(text)
    return "\n".join(lines), related


def _actions_text(trace: Iterable[Dict[str, Any]]) -> str:
    lines = []
    for call in trace:
        state = "ok" if call.get("ok") else "failed" if call.get("ok") is False else "no result"
        result = f": {call['result']}" if call.get("result") else ""
        if call.get("ok") and call.get("tool") in CONTENT_TOOLS:
            # What was read is its own document; the digest only says that it was.
            result = " (content kept as a document)"
        lines.append(f"- {call.get('tool')}({call.get('arguments')}) → {state}{result}")
    return "\n".join(lines)


def _tool_title(tool: str, arguments: Dict[str, Any]) -> str:
    target = next((str(arguments[k]) for k in ("url", "path", "file_path", "query", "task") if arguments.get(k)), "")
    return _clip(f"{tool}: {target}" if target else tool, 200)


async def ingest_task(db: AsyncSession, task_id: str) -> str:
    from app.openclaw_sessions import task_action_trace, task_tool_results

    task = await db.get(Task, task_id)
    if not ingestible(task):
        return "internal"
    w = _Writer(db)
    doc = await _task_document(w, task)
    messages = (
        await db.execute(select(TaskMessage).where(TaskMessage.task_id == task.id).order_by(TaskMessage.created_at))
    ).scalars().all()
    for message in messages:
        if _turn_kind(message) in TURN_KINDS and message.role in ("user", "assistant"):
            await ingest_turn(db, message.id, writer=w)
    run = (
        await db.execute(select(ProcessRun.id).where(ProcessRun.task_id == task.id).order_by(ProcessRun.started_at.desc()))
    ).scalars().first()
    common = {"task_id": task.id, "session_key": task.openclaw_session_key, "process_run_id": run,
              "source_at": task.updated_at or task.created_at}
    history, related = await _path_history(db, task)
    await w.unit(doc, await _task_section(w, doc, "Path history"), "path", chunk_text(history),
                 **common, meta={"kind": "path_history", "intake_decision_id": task.intake_decision_id})

    deliverables = (
        await db.execute(
            select(DeepDocument).where(DeepDocument.task_id == task.id, DeepDocument.kind == "deliverable")
            .order_by(DeepDocument.source_at)
        )
    ).scalars().all()
    if deliverables:
        listing = "\n".join(
            f"- “{d.title}” ({len(d.toc or [])} sections), delivered {_local(d.source_at)} JST" for d in deliverables
        )
        await w.unit(doc, await _task_section(w, doc, "Deliverables"), "deliverables", chunk_text(listing),
                     **common, meta={"kind": "deliverables"})

    trace = await asyncio.to_thread(task_action_trace, task.id, limit=200)
    if trace:
        await w.unit(doc, await _task_section(w, doc, "Actions"), "actions", chunk_text(_actions_text(trace)),
                     **common, meta={"kind": "actions", "calls": len(trace)})

    threshold = int(get_deep_memory_config().get("tool_document_min_chars") or 2000)
    results = await asyncio.to_thread(task_tool_results, task.id, tools=CONTENT_TOOLS, min_chars=threshold)
    seen: set = set()
    for result in results:
        page_title, body = readable_result(result["text"])
        digest = hashlib.sha256(body.encode()).hexdigest()
        if len(body) < threshold or digest in seen:
            continue
        seen.add(digest)
        read_at = (
            datetime.fromtimestamp(result["timestamp"] / 1000, tz=timezone.utc).replace(tzinfo=None)
            if result.get("timestamp") else task.updated_at
        )
        target = _tool_title(result["tool"], result["arguments"])
        title = _clip(f"{page_title} ({target})", 200) if page_title else target
        source = {"tool": result["tool"], "call_id": result["call_id"],
                  **{k: result["arguments"][k] for k in ("url", "path", "file_path") if result["arguments"].get(k)}}
        untrusted = result["tool"] in _WEB_TOOLS
        tool_doc = await _document_from_text(
            w,
            source_key=f"tool:{task.id}:{result['call_id']}",
            kind="tool_document",
            title=title,
            text=body,
            fields={"task_id": task.id, "session_key": task.openclaw_session_key, "source_at": read_at,
                    "source_ref": {**source, "untrusted": untrusted}, "process_run_id": run},
            chunk_fields={"task_id": task.id, "session_key": task.openclaw_session_key, "process_run_id": run,
                          "role": "tool", "source_at": read_at,
                          "meta": {"kind": "tool_document", "tool": result["tool"], "untrusted": untrusted}},
        )
        await w.link(tool_doc.id, doc.id, "part_of", tool=result["tool"])

    for other_id, relation in related:
        await w.link(doc.id, document_id(task_source_key(other_id)), relation)
    present = [
        s for s in (await db.execute(select(DeepSection).where(DeepSection.document_id == doc.id))).scalars().all()
    ]
    doc.toc = [
        {"section_id": s.id, "ordinal": s.ordinal, "path": s.path, "title": s.title, "level": s.level}
        for s in sorted(present, key=lambda s: s.ordinal)
    ]
    doc.meta = {
        **(doc.meta or {}),
        "status": task.status,
        "closed_reason": (task.supplementary_context or {}).get("closed_reason"),
        "task_type": task.task_type,
        "intake_decision_id": task.intake_decision_id,
        "ended_at": task.updated_at.isoformat() if task.updated_at else None,
    }
    await w.finish()
    children = (
        await db.execute(select(DeepDocument.id).where(DeepDocument.task_id == task.id, DeepDocument.id != doc.id))
    ).scalars().all()
    for doc_id in [*children, doc.id]:
        await enqueue(db, "enrich", doc_id)
    return "done"


def _attachment_path(raw: str) -> Optional[Path]:
    """Only files OpenClaw stored for an inbound message; any other path is refused."""
    try:
        path = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    root = (Path(OPENCLAW_HOME) / "media").resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


async def ingest_attachments(db: AsyncSession, message_id: str) -> str:
    message = await db.get(TaskMessage, message_id)
    if message is None:
        return "gone"
    task = await db.get(Task, message.task_id)
    if not ingestible(task):
        return "internal"
    w = _Writer(db)
    task_doc = await _task_document(w, task)
    ingested = 0
    for attachment in _attachments(message):
        name = str(attachment.get("name") or "")
        mime = str(attachment.get("type") or "")
        path = _attachment_path(str(attachment.get("path") or ""))
        if path is None or path.stat().st_size > ATTACHMENT_MAX_BYTES:
            continue
        if not (mime.startswith("text/") or mime in ("application/json", "application/xml")
                or path.suffix.lower() in TEXT_EXTENSIONS):
            continue
        text = redact_secrets(path.read_text(encoding="utf-8", errors="replace"))
        if not text.strip():
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()
        doc = await _document_from_text(
            w,
            source_key=f"attachment:{digest[:32]}",
            kind="attachment",
            title=_clip(name or path.name, 200),
            text=text,
            fields={"task_id": task.id, "session_key": task.openclaw_session_key, "source_at": message.created_at,
                    "source_ref": {"message_id": message.id, "name": name, "type": mime}},
            chunk_fields={"task_id": task.id, "session_key": task.openclaw_session_key, "message_id": message.id,
                          "role": "user", "source_at": message.created_at,
                          "meta": {"kind": "attachment", "name": name}},
        )
        await w.link(doc.id, task_doc.id, "part_of", message_id=message.id)
        await enqueue(db, "enrich", doc.id)
        ingested += 1
    await w.finish()
    return "done" if ingested else "no text attachments"


async def _enrich(db: AsyncSession, document_id: str) -> str:
    from app.deep_memory.enrich import enrich_document

    return await enrich_document(db, document_id)


async def _facts(db: AsyncSession, task_id: str) -> str:
    from app.deep_memory.facts import extract_task_facts

    return await extract_task_facts(db, task_id)


_HANDLERS = {
    "turn": ingest_turn, "task": ingest_task, "attachment": ingest_attachments, "enrich": _enrich, "facts": _facts,
}


def _next_utc_day(now: datetime) -> datetime:
    return datetime(now.year, now.month, now.day) + timedelta(days=1, minutes=5)


async def _claim(limit: int) -> List[Tuple[str, str, str]]:
    now = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        jobs = (
            await db.execute(
                select(DeepIngestJob)
                .where(DeepIngestJob.done_at.is_(None), DeepIngestJob.next_attempt_at <= now,
                       DeepIngestJob.kind.in_(tuple(_HANDLERS)))
                .order_by(DeepIngestJob.priority, DeepIngestJob.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for job in jobs:
            job.next_attempt_at = now + timedelta(seconds=CLAIM_LEASE_SEC)
        await db.commit()
        return [(job.id, job.kind, job.ref_id) for job in jobs]


async def ingest_once(limit: int = DRAIN_BATCH) -> Dict[str, int]:
    """Claim due jobs, then run each in its own transaction so a failure leaves nothing half-written."""
    stats = {"done": 0, "skipped": 0, "failed": 0}
    for job_id, kind, ref_id in await _claim(limit):
        try:
            async with AsyncSessionLocal() as db:
                outcome = await _HANDLERS[kind](db, ref_id)
                job = await db.get(DeepIngestJob, job_id)
                job.done_at = datetime.utcnow()
                job.last_error = None if outcome == "done" else outcome
                await db.commit()
        except Exception as exc:
            from app.llm.openai_direct import DirectBudgetExceeded

            async with AsyncSessionLocal() as db:
                job = await db.get(DeepIngestJob, job_id)
                if isinstance(exc, DirectBudgetExceeded):
                    # Not a failure: the day's model budget is spent, so the job waits for the next day.
                    job.last_error = f"deferred: {exc}"[:500]
                    job.next_attempt_at = _next_utc_day(datetime.utcnow())
                else:
                    job.attempts = (job.attempts or 0) + 1
                    job.last_error = (str(exc) or type(exc).__name__)[:500]
                    job.next_attempt_at = datetime.utcnow() + timedelta(
                        seconds=min(MAX_BACKOFF_SEC, 30 * 2 ** min(job.attempts, 7))
                    )
                await db.commit()
            stats["failed"] += 1
            logger.warning("Deep ingest %s %s failed (attempt %s): %s", kind, ref_id, job.attempts, exc)
            continue
        stats["done" if outcome == "done" else "skipped"] += 1
    return stats


async def deep_ingest_loop(stop_event: asyncio.Event) -> None:
    logger.info("Deep-memory ingest loop started (every %ss)", DRAIN_INTERVAL_SEC)
    while not stop_event.is_set():
        busy = False
        try:
            if is_enabled():
                stats = await ingest_once()
                busy = sum(stats.values()) >= DRAIN_BATCH
        except Exception:
            logger.exception("Deep-memory ingest loop error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.5 if busy else DRAIN_INTERVAL_SEC)
            break
        except asyncio.TimeoutError:
            pass
    logger.info("Deep-memory ingest loop stopped")
