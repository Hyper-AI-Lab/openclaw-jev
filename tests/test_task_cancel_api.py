"""POST /tasks/{id}/cancel: an active task is stopped, cancelled and indexed after the commit; a finished task changes
nothing, and the attempt is refused and logged through the guarded writer (R-22, R-04)."""
import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app import metrics
from app.api import server
from app.db.models import Base, Event, ProcessRun, Task

KEY = "task-cancel-key"
REFUSED = "task transition refused: {} {} -> cancelled actor=api:/tasks/{{id}}/cancel cause={}"


@pytest.fixture
async def api(tmp_path, monkeypatch):
    """The API on a fresh SQLite file. Temporal, the gateway abort and the registry index are fakes that record their
    calls, in order with the route's commits."""
    monkeypatch.setenv("RMP_METRICS_PATH", str(tmp_path / "metrics.json"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rmp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Task.__table__, ProcessRun.__table__, Event.__table__])
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    calls = []

    async def get_db():
        async with factory() as s:
            commit = s.commit

            async def recorded_commit():
                await commit()
                calls.append(("commit",))

            s.commit = recorded_commit
            yield s

    handle = MagicMock()
    handle.signal = AsyncMock(side_effect=lambda *args: calls.append(("signal", *args)))
    handle.terminate = AsyncMock(side_effect=lambda *args: calls.append(("terminate", *args)))
    client = MagicMock()
    client.get_workflow_handle.return_value = handle
    connect = AsyncMock(return_value=client)

    async def index(task_id):
        async with factory() as db:
            calls.append(("index", task_id, (await db.get(Task, task_id)).status))

    monkeypatch.setenv("RMP_API_KEY", KEY)
    monkeypatch.setattr(server, "connect_temporal", connect)
    monkeypatch.setattr("app.openclaw_control.schedule_abort",
                        lambda task_id, *, reason: calls.append(("abort", task_id, reason)))
    monkeypatch.setattr("app.task_registry.hooks.index_terminal_task_async", index)
    server.app.dependency_overrides[server.get_db] = get_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://rmp",
                                     headers={"X-RMP-API-Key": KEY}) as http:
            yield http, factory, calls, connect, handle
    finally:
        server.app.dependency_overrides.pop(server.get_db, None)
        await engine.dispose()


async def seed(factory, *rows):
    async with factory() as db:
        db.add_all(rows)
        await db.commit()


async def stored(factory, model, row_id):
    async with factory() as db:
        return await db.get(model, row_id)


async def cancelled_events(factory):
    async with factory() as db:
        return (await db.execute(select(Event).where(Event.event_type == "task.cancelled"))).scalars().all()


def refusals(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "rmp.transitions" and r.levelno == logging.WARNING]


async def test_cancel_indexes_the_task_after_commit(api):
    http, factory, calls, _connect, _handle = api
    await seed(factory, Task(id="t-cancel", goal="g", status="running", supplementary_context={"source": "slack"}),
               ProcessRun(id="r-cancel", task_id="t-cancel", current_state="running", lease_owner="w-1"))

    out = await http.post("/tasks/t-cancel/cancel", params={"reason": "canary_timeout"})

    assert out.status_code == 200 and out.json() == {"status": "cancelled", "task_id": "t-cancel"}
    task, run = await stored(factory, Task, "t-cancel"), await stored(factory, ProcessRun, "r-cancel")
    # stopped_by_user is reserved for Kirill's own "stop"; an API cancel records why.
    assert task.status == "cancelled"
    assert task.supplementary_context == {"source": "slack", "closed_reason": "canary_timeout"}
    assert run.current_state == "canceled" and run.ended_at is not None and run.lease_owner is None
    assert [e.event_payload for e in await cancelled_events(factory)] == [{"source": "api", "reason": "canary_timeout"}]
    assert calls == [("abort", "t-cancel", "canary_timeout"), ("signal", "cancel", "Cancelled via API"),
                     ("terminate", "Cancelled via API"), ("commit",), ("index", "t-cancel", "cancelled")]


async def test_cancel_on_a_completed_task_is_a_no_op(api, caplog):
    caplog.set_level(logging.WARNING, logger="rmp.transitions")
    http, factory, calls, connect, _handle = api
    await seed(factory, Task(id="t-done", goal="g", status="completed"),
               ProcessRun(id="r-done", task_id="t-done", current_state="completed"))

    out = await http.post("/tasks/t-done/cancel", params={"reason": "canary_timeout"})

    assert out.status_code == 200
    assert out.json() == {"status": "completed", "task_id": "t-done", "cancelled": False}
    assert (await stored(factory, Task, "t-done")).status == "completed"
    assert (await stored(factory, ProcessRun, "r-done")).current_state == "completed"
    assert await cancelled_events(factory) == []
    # No Temporal call, no abort, no index: only the route's commit.
    connect.assert_not_awaited()
    assert calls == [("commit",)]
    assert refusals(caplog) == [REFUSED.format("t-done", "completed", "canary_timeout")]
    assert metrics.get_counters()["task_transition_refused"] == 1


async def test_a_repeated_cancel_is_unchanged(api, caplog):
    caplog.set_level(logging.WARNING, logger="rmp.transitions")
    http, factory, calls, connect, _handle = api
    await seed(factory, Task(id="t-gone", goal="g", status="cancelled"))

    out = await http.post("/tasks/t-gone/cancel")

    assert out.status_code == 200
    assert out.json() == {"status": "cancelled", "task_id": "t-gone", "cancelled": False}
    connect.assert_not_awaited()
    assert calls == [("commit",)] and await cancelled_events(factory) == [] and refusals(caplog) == []


async def test_a_task_that_finishes_while_it_is_cancelled_keeps_its_status(api, caplog):
    """The workflow closed on Kirill's stop while the route was terminating it: the same write is refused, and the
    route answers with the status the task ended in."""
    caplog.set_level(logging.WARNING, logger="rmp.transitions")
    http, factory, calls, _connect, handle = api
    await seed(factory, Task(id="t-race", goal="g", status="running"))

    async def finished_meanwhile(reason):
        async with factory() as db:
            (await db.get(Task, "t-race")).status = "stopped_by_user"
            await db.commit()
        calls.append(("terminate", reason))

    handle.terminate.side_effect = finished_meanwhile

    out = await http.post("/tasks/t-race/cancel", params={"reason": "dashboard"})

    assert out.status_code == 200
    assert out.json() == {"status": "stopped_by_user", "task_id": "t-race", "cancelled": False}
    assert (await stored(factory, Task, "t-race")).status == "stopped_by_user"
    assert await cancelled_events(factory) == [] and not any(call[0] == "index" for call in calls)
    assert refusals(caplog) == [REFUSED.format("t-race", "stopped_by_user", "dashboard")]
