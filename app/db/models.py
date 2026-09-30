import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, JSON, Text, Index, text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def generate_uuid():
    return str(uuid.uuid4())


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_request_id = Column(String)
    correlation_id = Column(String)
    idempotency_key = Column(String, unique=True, index=True)
    requester = Column(String)
    openclaw_session_key = Column(String, index=True)
    task_type = Column(String, default="user")
    goal = Column(Text)
    status = Column(String, default="created", index=True)
    priority = Column(Integer, default=0)
    next_check_at = Column(DateTime)
    parent_task_id = Column(String, ForeignKey("tasks.id"), index=True)
    task_kind = Column(String, default="one_shot", index=True)  # one_shot | recurrent | durable
    recurrence_key = Column(String, index=True)
    intake_decision_id = Column(String, index=True)
    supplementary_context = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    process_runs = relationship("ProcessRun", back_populates="task")
    messages = relationship("TaskMessage", back_populates="task")


class ProcessRun(Base):
    __tablename__ = "process_runs"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id"), index=True)
    process_type = Column(String, default="generic_task")
    plan_version = Column(String, default="1")
    current_state = Column(String, default="created")
    success_criteria = Column(JSON)
    failure_criteria = Column(JSON)
    plan_json = Column(JSON)
    parent_process_run_id = Column(String, ForeignKey("process_runs.id"), index=True)
    next_check_at = Column(DateTime)
    lease_owner = Column(String)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime)
    task = relationship("Task", back_populates="process_runs")
    steps = relationship("Step", back_populates="process_run")
    observations = relationship("Observation", back_populates="process_run")


class Step(Base):
    __tablename__ = "steps"

    id = Column(String, primary_key=True, default=generate_uuid)
    process_run_id = Column(String, ForeignKey("process_runs.id"), index=True)
    step_name = Column(String)
    step_kind = Column(String)
    status = Column(String, default="pending")
    attempt_no = Column(Integer, default=1)
    idempotency_key = Column(String, index=True)
    input_ref = Column(JSON)
    output_ref = Column(JSON)
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    process_run = relationship("ProcessRun", back_populates="steps")


class Observation(Base):
    __tablename__ = "observations"

    id = Column(String, primary_key=True, default=generate_uuid)
    process_run_id = Column(String, ForeignKey("process_runs.id"), index=True)
    source = Column(String)
    observation_type = Column(String)
    payload_ref = Column(JSON)
    payload_hash = Column(String)
    observed_at = Column(DateTime, default=datetime.utcnow)
    confidence = Column(Integer, default=100)
    process_run = relationship("ProcessRun", back_populates="observations")


class Artifact(Base):
    __tablename__ = "artifacts"

    id = Column(String, primary_key=True, default=generate_uuid)
    process_run_id = Column(String, ForeignKey("process_runs.id"), index=True)
    kind = Column(String, index=True)
    uri = Column(String)
    checksum = Column(String, index=True)
    mime_type = Column(String)
    filename = Column(String)
    size_bytes = Column(Integer)
    storage_key = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class OsProcessTrack(Base):
    """Tracks Moltbook scanner OS processes as first-class RMP entities."""

    __tablename__ = "os_process_tracks"

    scanner_id = Column(String, primary_key=True)
    display_name = Column(String)
    script_basename = Column(String)
    script_path = Column(String)
    log_path = Column(String)
    task_id = Column(String, ForeignKey("tasks.id"), index=True)
    process_run_id = Column(String, ForeignKey("process_runs.id"), index=True)
    pid = Column(Integer)
    status = Column(String, default="stopped", index=True)
    last_log_mtime = Column(DateTime)
    last_started_at = Column(DateTime)
    last_seen_at = Column(DateTime)
    run_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MemoryItem(Base):
    __tablename__ = "memory_items"

    id = Column(String, primary_key=True, default=generate_uuid)
    scope_type = Column(String, index=True)  # process | task | user | procedural
    scope_id = Column(String, index=True)
    memory_type = Column(String)  # working | semantic | procedural | episodic
    content = Column(Text)
    content_ref = Column(JSON)
    provenance_ref = Column(JSON)
    confidence = Column(Integer, default=100)
    valid_from = Column(DateTime, default=datetime.utcnow)
    valid_to = Column(DateTime)
    supersedes_memory_id = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_memory_scope", "scope_type", "scope_id", "memory_type"),
    )


class MemoryLink(Base):
    __tablename__ = "memory_links"

    id = Column(String, primary_key=True, default=generate_uuid)
    source_id = Column(String, ForeignKey("memory_items.id"), index=True)
    target_id = Column(String, ForeignKey("memory_items.id"), index=True)
    relation = Column(String, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_memory_link_pair", "source_id", "target_id", "relation"),
    )


class SideEffectReceipt(Base):
    __tablename__ = "side_effect_receipts"

    id = Column(String, primary_key=True, default=generate_uuid)
    idempotency_key = Column(String, unique=True, index=True)
    effect_type = Column(String, index=True)
    metadata_ref = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)


class Event(Base):
    __tablename__ = "events"

    id = Column(String, primary_key=True, default=generate_uuid)
    correlation_id = Column(String, index=True)
    entity_type = Column(String)
    entity_id = Column(String, index=True)
    event_type = Column(String)
    event_payload = Column(JSON)
    occurred_at = Column(DateTime, default=datetime.utcnow)


class VectorOutbox(Base):
    """Qdrant index work, committed in the same transaction as the Postgres row it indexes."""

    __tablename__ = "vector_outbox"

    id = Column(String, primary_key=True, default=generate_uuid)
    kind = Column(String, nullable=False)  # memory | registry
    ref_id = Column(String, nullable=False, index=True)  # memory_items.id | tasks.id
    attempts = Column(Integer, default=0)
    last_error = Column(Text)
    next_attempt_at = Column(DateTime, default=datetime.utcnow, index=True)
    done_at = Column(DateTime, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TaskMessage(Base):
    """The conversation log: every message of a task, Kirill's and Aura's, with its metadata."""

    __tablename__ = "task_messages"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id"), index=True, nullable=False)
    role = Column(String, default="user")  # user | assistant | evaluator | cron | canary | <catalog type>
    content = Column(Text, nullable=False)
    source = Column(String, default="api")  # slack | cron | api | signal | process_evaluator
    slack_ts = Column(String, index=True)  # Slack message ts, for replies and threads
    # request | attached | clarify_answer | reply | followup | notice | verdict
    kind = Column(String, index=True)
    session_key = Column(String, index=True)
    # thread, reply_to, attachments, intake decision, process run, attempt
    meta = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="messages")

    __table_args__ = (Index("ix_task_messages_task_created", "task_id", "created_at"),)


class TaskIntakeDecision(Base):
    """Audit log for universal task intake adjudication."""

    __tablename__ = "task_intake_decisions"

    id = Column(String, primary_key=True, default=generate_uuid)
    request_hash = Column(String, index=True)
    decision = Column(String, index=True)
    confidence = Column(Integer, default=0)
    rationale = Column(Text)
    similar_task_ids = Column(JSON)
    llm_raw = Column(JSON)
    policy_overrides = Column(JSON)
    intake_mode = Column(String, default="shadow")  # off | shadow | enforce
    session_key = Column(String)
    intent_snippet = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class TaskRegistryEntry(Base):
    """Denormalized task summary for registry search and RAG indexing."""

    __tablename__ = "task_registry_entries"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id"), unique=True, index=True)
    intent_snippet = Column(Text)
    outcome_summary = Column(Text)
    process_type = Column(String, index=True)
    terminal_status = Column(String, index=True)
    task_kind = Column(String)
    recurrence_key = Column(String, index=True)
    session_key = Column(String, index=True)
    duration_sec = Column(Integer)
    artifact_refs = Column(JSON)
    vector_point_id = Column(String)
    indexed_at = Column(DateTime, default=datetime.utcnow)
    task_created_at = Column(DateTime)
    task_ended_at = Column(DateTime)

    __table_args__ = (
        Index("ix_task_registry_recurrence", "recurrence_key", "terminal_status"),
    )


class DeepDocument(Base):
    """A body of text in deep memory, with its summary and table of contents.

    One per user task (its conversation, path history, deliverables and actions),
    one per long deliverable, one per page or file Aura read, one per text attachment.
    """

    __tablename__ = "dm_documents"

    id = Column(String, primary_key=True, default=generate_uuid)
    kind = Column(String, nullable=False, index=True)  # task | deliverable | tool_document | attachment
    # What the document was built from, e.g. task:<id>, deliverable:<message id>; one document each.
    source_key = Column(String, nullable=False, unique=True)
    title = Column(Text)
    task_id = Column(String, ForeignKey("tasks.id"), index=True)
    session_key = Column(String, index=True)
    process_run_id = Column(String, index=True)
    source_ref = Column(JSON)  # message ids, url, path, tool name
    content_hash = Column(String)
    summary = Column(Text)
    toc = Column(JSON)  # [{section_id, ordinal, path, title, summary}]
    status = Column(String, default="raw", index=True)  # raw | enriched | failed
    meta = Column(JSON)
    source_at = Column(DateTime, index=True)  # when the content was said or read
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    enriched_at = Column(DateTime)
    valid_to = Column(DateTime)


class DeepSection(Base):
    """One entry of a document's table of contents."""

    __tablename__ = "dm_sections"

    id = Column(String, primary_key=True)
    document_id = Column(String, ForeignKey("dm_documents.id"), index=True, nullable=False)
    ordinal = Column(Integer, nullable=False)
    path = Column(Text)  # "Networking > Ingress"
    title = Column(Text)
    level = Column(Integer, default=1)
    summary = Column(Text)
    chunk_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DeepChunk(Base):
    """A retrievable passage, pointing at its section and carrying its conversation metadata."""

    __tablename__ = "dm_chunks"

    id = Column(String, primary_key=True)
    document_id = Column(String, ForeignKey("dm_documents.id"), index=True, nullable=False)
    section_id = Column(String, ForeignKey("dm_sections.id"), index=True)
    ordinal = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    context_header = Column(Text)  # situates the chunk in its document (contextual retrieval)
    char_count = Column(Integer)
    task_id = Column(String, index=True)
    session_key = Column(String, index=True)
    process_run_id = Column(String)
    message_id = Column(String, index=True)
    role = Column(String)
    source_at = Column(DateTime, index=True)
    meta = Column(JSON)  # kind, attempt, intake decision, tool, url
    created_at = Column(DateTime, default=datetime.utcnow)
    valid_to = Column(DateTime)

    __table_args__ = (Index("ix_dm_chunks_document_ordinal", "document_id", "ordinal"),)


class DeepLink(Base):
    """A typed edge between deep-memory objects, e.g. a task that continues another."""

    __tablename__ = "dm_links"

    id = Column(String, primary_key=True, default=generate_uuid)
    source_type = Column(String, nullable=False)  # document | chunk | memory
    source_id = Column(String, nullable=False, index=True)
    target_type = Column(String, nullable=False)
    target_id = Column(String, nullable=False, index=True)
    relation = Column(String, nullable=False, index=True)  # continues | related | refines | derived_from
    meta = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ux_dm_links_edge", "source_id", "target_id", "relation", unique=True),
    )


class DeepIngestJob(Base):
    """Deep-memory ingestion work, committed with the row it ingests."""

    __tablename__ = "dm_ingest_queue"

    id = Column(String, primary_key=True, default=generate_uuid)
    kind = Column(String, nullable=False)  # turn | task | attachment | enrich | facts
    ref_id = Column(String, nullable=False, index=True)
    priority = Column(Integer, default=5)  # lower runs first
    attempts = Column(Integer, default=0)
    last_error = Column(Text)
    next_attempt_at = Column(DateTime, default=datetime.utcnow)
    done_at = Column(DateTime, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index(
            "ux_dm_ingest_pending",
            "kind",
            "ref_id",
            unique=True,
            postgresql_where=text("done_at IS NULL"),
            sqlite_where=text("done_at IS NULL"),
        ),
        Index(
            "ix_dm_ingest_due",
            "priority",
            "next_attempt_at",
            postgresql_where=text("done_at IS NULL"),
            sqlite_where=text("done_at IS NULL"),
        ),
    )


class DeepContextReport(Base):
    """What the IA's deep recall found for a task, and what became of it."""

    __tablename__ = "dm_context_reports"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id"), index=True, nullable=False)
    trigger = Column(String)  # task_start | leg
    status = Column(String, default="running", index=True)  # running | ready | empty | failed | skipped
    queries = Column(JSON)
    candidate_ids = Column(JSON)
    report = Column(JSON)
    brief = Column(Text)
    latency_ms = Column(Integer)
    usage = Column(JSON)
    consumed_by = Column(String)  # steps | evaluator | followup | none
    novelty = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)
