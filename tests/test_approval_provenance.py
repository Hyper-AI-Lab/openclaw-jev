"""Before a deploy, an approval must be a Slack message from Kirill's user, after the gate opened."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import ActivityEnvironment

from app import config
from app.activities import db_activities
from app.db.models import Base, Event, Task, TaskMessage
from app.task_registry.intake_handlers import _slack_meta

OWNER = "U0OWNER01"
OPENED = datetime(2026, 10, 1, 9, 0, 0)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rmp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_activities, "AsyncSessionLocal", maker)
    monkeypatch.setattr(config, "get_slack_owner_user_id", lambda: OWNER)
    async with maker() as session:
        session.add(Task(id="t1", goal="Ship the fix", status="pending_user_input"))
        await session.commit()
    yield maker
    await engine.dispose()


async def _message(maker, text, *, user=OWNER, minutes=5, source="slack"):
    async with maker() as session:
        slack = {"message_id": "1790000000.000100", "user_id": user, "event_ts": 1790000000123} if source == "slack" else None
        session.add(TaskMessage(task_id="t1", role="user", content=text, source=source, kind="attached",
                                slack_ts="1790000000.000100" if source == "slack" else None,
                                meta={"slack": slack}, created_at=OPENED + timedelta(minutes=minutes)))
        await session.commit()


async def _confirm():
    return await ActivityEnvironment().run(db_activities.confirm_approval_provenance,
                                           {"task_id": "t1", "gate_opened_at": OPENED.isoformat() + "+00:00"})


async def _refusals(maker):
    async with maker() as session:
        return (await session.execute(select(Event).where(Event.event_type == "approval.refused"))).scalars().all()


async def test_kirills_own_slack_approval_after_the_gate_opened_is_confirmed(db):
    await _message(db, "approve")
    result = await _confirm()
    assert result["ok"] and result["slack_user_id"] == OWNER and result["slack_ts"] == "1790000000.000100"
    assert result["event_ts"] == 1790000000123 and await _refusals(db) == []


@pytest.mark.parametrize("kwargs", [
    {"text": "approve", "user": "U0SOMEONE"},
    {"text": "approve", "minutes": -5},
    {"text": "approve", "source": "signal"},
    {"text": "approve, but use the cheaper vendor"},
])
async def test_anything_else_is_refused_and_the_refusal_recorded(db, kwargs):
    text = kwargs.pop("text")
    await _message(db, text, **kwargs)
    result = await _confirm()
    assert not result["ok"] and "no Slack approval from U0OWNER01" in result["reason"]
    (refusal,) = await _refusals(db)
    assert refusal.entity_id == "t1" and refusal.event_payload["reason"] == result["reason"]


def test_the_attach_path_records_who_sent_the_message_and_when():
    request = SimpleNamespace(slack_message_id="1790000000.000100", slack_user_id=OWNER, slack_event_ts=1790000000123,
                              thread_id=None, reply_to=None, attachments=None)
    meta = _slack_meta(request)
    assert (meta["user_id"], meta["event_ts"], meta["message_id"]) == (OWNER, 1790000000123, "1790000000.000100")
