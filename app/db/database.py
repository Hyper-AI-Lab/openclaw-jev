import os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.db.models import Base

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://rmp:rmp_password@localhost/rmp_db"
)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Columns added after initial deploy — applied idempotently at startup.
_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS next_check_at TIMESTAMP",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_tasks_idempotency_key ON tasks (idempotency_key)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks (status)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_session ON tasks (openclaw_session_key)",
    "ALTER TABLE process_runs ADD COLUMN IF NOT EXISTS lease_owner VARCHAR",
    "ALTER TABLE steps ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR",
    "ALTER TABLE observations ADD COLUMN IF NOT EXISTS payload_hash VARCHAR",
    "ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS filename VARCHAR",
    "ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS size_bytes INTEGER",
    "ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS storage_key VARCHAR",
    "CREATE INDEX IF NOT EXISTS ix_artifacts_checksum ON artifacts (checksum)",
    "CREATE INDEX IF NOT EXISTS ix_artifacts_kind ON artifacts (kind)",
    "CREATE INDEX IF NOT EXISTS ix_os_process_tracks_status ON os_process_tracks (status)",
    """CREATE TABLE IF NOT EXISTS memory_links (
        id VARCHAR PRIMARY KEY,
        source_id VARCHAR REFERENCES memory_items(id),
        target_id VARCHAR REFERENCES memory_items(id),
        relation VARCHAR,
        created_at TIMESTAMP DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_memory_links_source ON memory_links (source_id)",
    "CREATE INDEX IF NOT EXISTS ix_memory_links_target ON memory_links (target_id)",
    "CREATE INDEX IF NOT EXISTS ix_memory_links_relation ON memory_links (relation)",
    "CREATE INDEX IF NOT EXISTS ix_memory_link_pair ON memory_links (source_id, target_id, relation)",
    """CREATE TABLE IF NOT EXISTS side_effect_receipts (
        id VARCHAR PRIMARY KEY,
        idempotency_key VARCHAR UNIQUE,
        effect_type VARCHAR,
        metadata_ref JSON,
        created_at TIMESTAMP DEFAULT NOW()
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_side_effect_idempotency ON side_effect_receipts (idempotency_key)",
    "ALTER TABLE process_runs ADD COLUMN IF NOT EXISTS plan_json JSON",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS parent_task_id VARCHAR",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_kind VARCHAR DEFAULT 'one_shot'",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS recurrence_key VARCHAR",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS intake_decision_id VARCHAR",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS supplementary_context JSON",
    "CREATE INDEX IF NOT EXISTS ix_tasks_parent_task_id ON tasks (parent_task_id)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_task_kind ON tasks (task_kind)",
    "CREATE INDEX IF NOT EXISTS ix_tasks_recurrence_key ON tasks (recurrence_key)",
    """CREATE TABLE IF NOT EXISTS task_intake_decisions (
        id VARCHAR PRIMARY KEY,
        request_hash VARCHAR,
        decision VARCHAR,
        confidence INTEGER DEFAULT 0,
        rationale TEXT,
        similar_task_ids JSON,
        llm_raw JSON,
        policy_overrides JSON,
        intake_mode VARCHAR DEFAULT 'shadow',
        session_key VARCHAR,
        intent_snippet VARCHAR,
        created_at TIMESTAMP DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS task_messages (
        id VARCHAR PRIMARY KEY,
        task_id VARCHAR NOT NULL REFERENCES tasks(id),
        role VARCHAR DEFAULT 'user',
        content TEXT NOT NULL,
        source VARCHAR DEFAULT 'api',
        created_at TIMESTAMP DEFAULT NOW()
    )""",
    "ALTER TABLE process_runs ADD COLUMN IF NOT EXISTS parent_process_run_id VARCHAR",
    "CREATE INDEX IF NOT EXISTS ix_process_runs_parent_process_run_id ON process_runs (parent_process_run_id)",
    """CREATE TABLE IF NOT EXISTS task_registry_entries (
        id VARCHAR PRIMARY KEY,
        task_id VARCHAR UNIQUE NOT NULL REFERENCES tasks(id),
        intent_snippet TEXT,
        outcome_summary TEXT,
        process_type VARCHAR,
        terminal_status VARCHAR,
        task_kind VARCHAR,
        recurrence_key VARCHAR,
        session_key VARCHAR,
        duration_sec INTEGER,
        artifact_refs JSON,
        vector_point_id VARCHAR,
        indexed_at TIMESTAMP DEFAULT NOW(),
        task_created_at TIMESTAMP,
        task_ended_at TIMESTAMP
    )""",
]


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in _MIGRATIONS:
            await conn.execute(text(stmt))


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
