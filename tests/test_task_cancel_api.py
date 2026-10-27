"""POST /tasks/{id}/cancel: an active task is cancelled through temporal_control.cancel_task, which writes the status
first, then stops the task and indexes it; a finished task changes nothing, and the attempt is refused and logged
through the guarded writer (R-22, R-04, TE-11)."""
import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from temporalio.service import RPCError, RPCStatusCode

from app import metrics, temporal_control
from app.api import server
from app.db.models import Base, Event, ProcessRun, Task

KEY = "task-cancel-key"
REFUSED = "task transition refused: {} {} -> cancelled actor=api:/tasks/{{id}}/cancel cause={}"


@pytest.fixture
async def api(tmp_path, monkeypatch):
    """The API on a fresh SQLite file. Temporal, the gateway abort and the registry index are fakes that record their
    calls, in order with the commits of the route's session and of cancel_task's."""
    monkeypatch.setenv("RMP_METRICS_PATH", str(tmp_path / "metrics.json"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rmp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Task.__table__, ProcessRun.__table__, Event.__table__])
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    calls = []

    def recording():
        session = factory()
        commit = session.commit

        async def recorded_commit():
            await commit()
            calls.append(("commit",))

        session.commit = recorded_commit
        return session

    async def get_db():
        async with recording() as s:
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
    monkeypatch.setattr(temporal_control, "connect_temporal", connect)
    monkeypatch.setattr(temporal_control, "AsyncSessionLocal", recording)
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
    assert [e.event_payload for e in await cancelled_events(factory)] == [
        {"source": "api:/tasks/{id}/cancel", "reason": "canary_timeout"}]
    # The status is committed first: only the call that cancelled the task stops it.
    assert calls == [("commit",), ("abort", "t-cancel", "canary_timeout"), ("signal", "cancel", "canary_timeout"),
                     ("terminate", "canary_timeout"), ("index", "t-cancel", "cancelled")]


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


async def test_a_task_that_finishes_while_it_is_cancelled_keeps_its_status(api, caplog, monkeypatch):
    """The workflow closed on Kirill's stop between the route's read and the cancel's write: that write is refused,
    nothing is aborted or terminated, and the route answers with the status the task ended in."""
    caplog.set_level(logging.WARNING, logger="rmp.transitions")
    http, factory, calls, connect, _handle = api
    await seed(factory, Task(id="t-race", goal="g", status="running"))
    write = temporal_control.set_task_status

    async def finished_meanwhile(db, task_id, to, **kwargs):
        async with factory() as other:
            (await other.get(Task, "t-race")).status = "stopped_by_user"
            await other.commit()
        return await write(db, task_id, to, **kwargs)

    monkeypatch.setattr(temporal_control, "set_task_status", finished_meanwhile)

    out = await http.post("/tasks/t-race/cancel", params={"reason": "dashboard"})

    assert out.status_code == 200
    assert out.json() == {"status": "stopped_by_user", "task_id": "t-race", "cancelled": False}
    assert (await stored(factory, Task, "t-race")).status == "stopped_by_user"
    assert await cancelled_events(factory) == []
    connect.assert_not_awaited()
    assert calls == [("commit",)]
    assert refusals(caplog) == [REFUSED.format("t-race", "stopped_by_user", "dashboard")]


async def test_cancel_task_terminates_and_reports_the_real_change(api):
    """TE-11: cancel_task is True only for the call that moved the task to cancelled; that call ends its run and
    terminates workflow-<id> inside the running loop. A finished task is False, and nothing is called."""
    _http, factory, calls, connect, handle = api
    await seed(factory, Task(id="t-live", goal="g", status="running"),
               ProcessRun(id="r-live", task_id="t-live", current_state="running", lease_owner="w-1"),
               Task(id="t-comp", goal="g", status="compensated"))

    assert await temporal_control.cancel_task("t-live", reason="canary_timeout", actor="canary_sentinel") is True

    task, run = await stored(factory, Task, "t-live"), await stored(factory, ProcessRun, "r-live")
    assert task.status == "cancelled" and task.supplementary_context == {"closed_reason": "canary_timeout"}
    assert run.current_state == "canceled" and run.ended_at is not None and run.lease_owner is None
    assert [e.event_payload for e in await cancelled_events(factory)] == [
        {"source": "canary_sentinel", "reason": "canary_timeout"}]
    connect.return_value.get_workflow_handle.assert_called_once_with("workflow-t-live")
    handle.terminate.assert_awaited_once_with("canary_timeout")
    assert calls == [("commit",), ("abort", "t-live", "canary_timeout"), ("signal", "cancel", "canary_timeout"),
                     ("terminate", "canary_timeout"), ("index", "t-live", "cancelled")]

    calls.clear()
    connect.reset_mock()
    assert await temporal_control.cancel_task("t-comp", reason="canary_timeout", actor="canary_sentinel") is False
    assert (await stored(factory, Task, "t-comp")).status == "compensated"
    connect.assert_not_awaited()
    assert calls == [("commit",)] and len(await cancelled_events(factory)) == 1


@pytest.mark.parametrize("status, message, warnings", [
    (RPCStatusCode.NOT_FOUND, "workflow execution not found", []),
    (RPCStatusCode.UNAVAILABLE, "Temporal is unavailable", ["Terminate workflow t-live: Temporal is unavailable"]),
], ids=["not found", "unavailable"])
async def test_a_failed_terminate_is_a_warning_unless_the_workflow_is_gone(api, caplog, status, message, warnings):
    """NOT_FOUND on the terminate means the workflow is already closed or gone, which is what the cancel wanted, so it
    is no warning; any other RPC error is one WARNING. Either way the call that cancelled the task returns True."""
    caplog.set_level(logging.WARNING)
    _http, factory, calls, _connect, handle = api
    await seed(factory, Task(id="t-live", goal="g", status="running"))
    handle.terminate.side_effect = RPCError(message, status, b"")

    assert await temporal_control.cancel_task("t-live", reason="dashboard", actor="api:/tasks/{id}/cancel") is True

    assert (await stored(factory, Task, "t-live")).status == "cancelled"
    handle.terminate.assert_awaited_once_with("dashboard")
    assert calls == [("commit",), ("abort", "t-live", "dashboard"), ("signal", "cancel", "dashboard"),
                     ("index", "t-live", "cancelled")]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == warnings


async def test_a_waiting_cancel_awaits_the_abort(api, monkeypatch):
    """The sentinel's cancel (wait_abort) waits for Aura's runs to abort before it terminates: its process ends
    right after."""
    _http, factory, calls, _connect, _handle = api
    await seed(factory, Task(id="t-canary", goal="g", task_type="canary", status="running"))

    async def abort_task_runs(task_id, *, reason):
        calls.append(("abort awaited", task_id, reason))
        return []

    monkeypatch.setattr("app.openclaw_control.abort_task_runs", abort_task_runs)

    assert await temporal_control.cancel_task("t-canary", reason="health_canary_timeout", actor="canary_sentinel",
                                              wait_abort=True)
    assert calls == [("commit",), ("abort awaited", "t-canary", "health_canary_timeout"),
                     ("signal", "cancel", "health_canary_timeout"), ("terminate", "health_canary_timeout"),
                     ("index", "t-canary", "cancelled")]
