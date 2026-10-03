"""The Slack ledger for RMP's own notices: an id with no tasks row must still record slack.delivered."""
import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.activities import side_effects
from app.db.models import Base, DeepIngestJob, Event, SideEffectReceipt, Task, TaskMessage
from app.production import ops_notify

REAL_CLIENT = httpx.AsyncClient


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """A real database with foreign keys enforced, as Postgres does; SQLite ignores them unless asked."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ledger.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[m.__table__ for m in (Task, Event, SideEffectReceipt, TaskMessage, DeepIngestJob)])
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(side_effects, "AsyncSessionLocal", factory)
    async with factory() as s:
        s.add(Task(id="real-task", goal="Plan the trip", task_type="user", status="running"))
        await s.commit()
    yield factory
    await engine.dispose()


async def test_an_ops_notice_records_slack_delivered_though_its_id_has_no_task(db, monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True, "ts": "1790.0001"}))
    monkeypatch.setattr(side_effects.httpx, "AsyncClient", lambda *a, **k: REAL_CLIENT(transport=transport))
    monkeypatch.setattr(ops_notify, "get_ops_slack_config", lambda: {"enabled": True, "session_key": "k"})
    monkeypatch.setattr(ops_notify, "get_slack_bot_token", lambda: "xoxb-test")
    monkeypatch.setattr(ops_notify, "_get_slack_user_id", lambda session_key: "U1")

    assert await ops_notify.notify_ops_slack("Canary failed twice", incident_id="canary-x") is True

    async with db() as s:
        delivered = (await s.execute(select(Event).where(Event.event_type == "slack.delivered"))).scalars().all()
        receipts = (await s.execute(select(SideEffectReceipt))).scalars().all()
        messages = (await s.execute(select(TaskMessage))).scalars().all()
    assert len(receipts) == 1  # the Slack send itself is recorded, so a retry will not repeat it
    assert [e.entity_id for e in delivered] == ["ops:canary-x"]
    assert messages == []  # a notice has no task, so no task message may be attempted


async def test_a_real_tasks_reply_is_recorded_as_before(db, monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True, "ts": "1790.0002"}))
    monkeypatch.setattr(side_effects.httpx, "AsyncClient", lambda *a, **k: REAL_CLIENT(transport=transport))

    assert await side_effects.send_slack_message_idempotent(
        "real-task", "U1", "Osaka is mild in October.", "xoxb-test",
        kind="reply", session_key="agent:main:slack:channel:d0test", meta={"attempt": 2},
    ) is True

    async with db() as s:
        delivered = (await s.execute(select(Event).where(Event.event_type == "slack.delivered"))).scalars().all()
        messages = (await s.execute(select(TaskMessage))).scalars().all()
        jobs = (await s.execute(select(DeepIngestJob))).scalars().all()
    assert [(e.entity_id, e.event_payload) for e in delivered] == [("real-task", {"user_id": "U1", "parts": 1})]
    [m] = messages
    assert (m.task_id, m.role, m.content, m.source, m.slack_ts, m.kind, m.session_key, m.meta) == (
        "real-task", "assistant", "Osaka is mild in October.", "slack", "1790.0002", "reply",
        "agent:main:slack:channel:d0test", {"attempt": 2, "parts": 1})
    assert [(j.kind, j.ref_id) for j in jobs] == [("turn", m.id)]
