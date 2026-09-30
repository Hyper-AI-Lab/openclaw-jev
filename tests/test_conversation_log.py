"""Conversation-log metadata, deep-memory tables and the deep_memory settings section."""
import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import database
from app.db.models import Base, DeepIngestJob, Task, TaskMessage
from app.llm import openai_direct
from app.task_registry import messages


@pytest.fixture
async def sessions(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rmp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(messages, "AsyncSessionLocal", maker)
    yield maker
    await engine.dispose()


async def test_deep_memory_tables_exist_and_a_pending_job_is_queued_once(sessions):
    async with sessions() as db:
        db.add(DeepIngestJob(kind="turn", ref_id="m1"))
        await db.commit()
        db.add(DeepIngestJob(kind="turn", ref_id="m1"))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
        job = (await db.execute(select(DeepIngestJob))).scalar_one()
        job.done_at = datetime.utcnow()
        await db.commit()
        # Once done, the same source may be queued again (for example after an edit).
        db.add(DeepIngestJob(kind="turn", ref_id="m1"))
        await db.commit()
        assert len((await db.execute(select(DeepIngestJob))).scalars().all()) == 2


async def test_a_message_keeps_its_kind_session_and_only_meaningful_metadata(sessions):
    async with sessions() as db:
        db.add(Task(id="t1", goal="g", openclaw_session_key="agent:main:slack:channel:d1"))
        await db.commit()
    msg_id = await messages.add_task_message(
        "t1",
        "Add Kobe beef at lunch.",
        kind="attached",
        session_key="agent:main:slack:channel:d1",
        meta={"intake_decision_id": "d-1", "slack": None, "targets": [], "attempt": 0},
    )
    async with sessions() as db:
        row = await db.get(TaskMessage, msg_id)
    assert (row.kind, row.session_key) == ("attached", "agent:main:slack:channel:d1")
    assert row.meta == {"intake_decision_id": "d-1", "attempt": 0}
    bare = await messages.add_task_message("t1", "hello")
    async with sessions() as db:
        row = await db.get(TaskMessage, bare)
    assert (row.kind, row.session_key, row.meta) == ("request", None, None)


def test_migrations_add_the_log_columns_and_deep_memory_text_search():
    ddl = "\n".join(database._MIGRATIONS)
    for column in ("kind VARCHAR", "session_key VARCHAR", "meta JSON"):
        assert f"ALTER TABLE task_messages ADD COLUMN IF NOT EXISTS {column}" in ddl
    for index in ("ix_dm_chunks_fts", "ix_dm_sections_fts", "ix_dm_documents_fts"):
        assert f"CREATE INDEX IF NOT EXISTS {index}" in ddl
    assert all("IF NOT EXISTS" in stmt for stmt in database._MIGRATIONS[-8:])


def test_deep_memory_settings_merge_over_defaults(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"api_key": "k", "deep_memory": {"followups_enabled": True, "lane_concurrency": 5}}))
    monkeypatch.setattr(config, "SETTINGS_PATH", str(path))
    cfg = config.get_deep_memory_config()
    assert cfg["followups_enabled"] is True and cfg["lane_concurrency"] == 5
    assert cfg["recall_enabled"] is False and cfg["collection_name"] == "rmp_deep_memory_v1"
    assert set(config.DEFAULT_DEEP_MEMORY) <= set(cfg)
    assert openai_direct.lane_policy().concurrency == 5


def test_invalid_lane_settings_fall_back_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"api_key": "k", "deep_memory": {"lane_concurrency": "many"}}))
    monkeypatch.setattr(config, "SETTINGS_PATH", str(path))
    assert openai_direct.lane_policy() == openai_direct.LanePolicy()


def test_settings_example_carries_every_deep_memory_default():
    example = json.loads((Path(__file__).resolve().parents[1] / "settings.example.json").read_text())
    assert example["deep_memory"] == config.DEFAULT_DEEP_MEMORY
