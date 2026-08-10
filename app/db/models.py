import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, JSON, Text, Index
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


class TaskMessage(Base):
    """Supplementary user/cron text linked to a task (not the primary RAG corpus)."""

    __tablename__ = "task_messages"

    id = Column(String, primary_key=True, default=generate_uuid)
    task_id = Column(String, ForeignKey("tasks.id"), index=True, nullable=False)
    role = Column(String, default="user")  # user | system | cron | api
    content = Column(Text, nullable=False)
    source = Column(String, default="api")  # slack | cron | api | signal
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
