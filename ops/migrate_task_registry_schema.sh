#!/usr/bin/env bash
# Idempotent schema migration for Phase 4 task registry tables/columns.
set -euo pipefail

RMP_ROOT="/root/.openclaw/rmp"
export PGPASSWORD="${PGPASSWORD:-rmp_password}"

psql -h 127.0.0.1 -U rmp -d rmp_db <<'SQL'
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS parent_task_id VARCHAR;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_kind VARCHAR DEFAULT 'one_shot';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS recurrence_key VARCHAR;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS intake_decision_id VARCHAR;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS supplementary_context JSON;

CREATE INDEX IF NOT EXISTS ix_tasks_parent_task_id ON tasks (parent_task_id);
CREATE INDEX IF NOT EXISTS ix_tasks_task_kind ON tasks (task_kind);
CREATE INDEX IF NOT EXISTS ix_tasks_recurrence_key ON tasks (recurrence_key);
CREATE INDEX IF NOT EXISTS ix_tasks_intake_decision_id ON tasks (intake_decision_id);

UPDATE tasks SET task_kind = 'one_shot' WHERE task_kind IS NULL;
UPDATE tasks SET task_kind = 'recurrent'
  WHERE task_kind = 'one_shot'
    AND task_type IN ('cron', 'canary', 'heartbeat');

CREATE TABLE IF NOT EXISTS task_intake_decisions (
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
);
CREATE INDEX IF NOT EXISTS ix_task_intake_decisions_request_hash ON task_intake_decisions (request_hash);
CREATE INDEX IF NOT EXISTS ix_task_intake_decisions_decision ON task_intake_decisions (decision);

CREATE TABLE IF NOT EXISTS task_messages (
    id VARCHAR PRIMARY KEY,
    task_id VARCHAR NOT NULL REFERENCES tasks(id),
    role VARCHAR DEFAULT 'user',
    content TEXT NOT NULL,
    source VARCHAR DEFAULT 'api',
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_task_messages_task_id ON task_messages (task_id);
CREATE INDEX IF NOT EXISTS ix_task_messages_task_created ON task_messages (task_id, created_at);

CREATE TABLE IF NOT EXISTS task_registry_entries (
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
);
CREATE INDEX IF NOT EXISTS ix_task_registry_entries_task_id ON task_registry_entries (task_id);
CREATE INDEX IF NOT EXISTS ix_task_registry_entries_process_type ON task_registry_entries (process_type);
CREATE INDEX IF NOT EXISTS ix_task_registry_entries_terminal_status ON task_registry_entries (terminal_status);
CREATE INDEX IF NOT EXISTS ix_task_registry_entries_recurrence_key ON task_registry_entries (recurrence_key);
CREATE INDEX IF NOT EXISTS ix_task_registry_entries_session_key ON task_registry_entries (session_key);
CREATE INDEX IF NOT EXISTS ix_task_registry_recurrence ON task_registry_entries (recurrence_key, terminal_status);

ALTER TABLE process_runs ADD COLUMN IF NOT EXISTS parent_process_run_id VARCHAR;
CREATE INDEX IF NOT EXISTS ix_process_runs_parent_process_run_id ON process_runs (parent_process_run_id);
SQL

echo "Task registry schema migration complete."
