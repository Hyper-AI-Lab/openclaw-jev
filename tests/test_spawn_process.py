"""Spawn process tests."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, ProcessRun, Task


def _mock_session(task, latest_run):
    task_result = MagicMock()
    task_result.scalar_one_or_none.return_value = task
    pr_result = MagicMock()
    pr_result.scalar_one_or_none.return_value = latest_run
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[task_result, pr_result])
    return session


@pytest.mark.asyncio
async def test_spawn_active_non_durable_returns_exists():
    from app.task_registry.spawn import spawn_process_for_task

    task = MagicMock()
    task.id = "t1"
    task.task_kind = "one_shot"
    task.task_type = "user"
    task.goal = "test"
    task.status = "running"
    task.parent_task_id = None
    task.openclaw_session_key = "agent:main:main"
    task.correlation_id = "t1"

    run = MagicMock()
    run.current_state = "running"

    with patch(
        "app.task_registry.spawn.workflow_is_running",
        new=AsyncMock(return_value=False),
    ):
        out = await spawn_process_for_task("t1", db=_mock_session(task, run))
    assert out.get("reason") == "active_process_exists"
    assert out.get("spawned") is False


@pytest.mark.asyncio
async def test_spawn_durable_running_signals_spawn_leg():
    from app.task_registry.spawn import spawn_process_for_task

    task = MagicMock()
    task.id = "t-durable"
    task.task_kind = "durable"
    task.task_type = "user"
    task.goal = "leg one"
    task.status = "running"
    task.openclaw_session_key = "agent:main:main"
    task.correlation_id = "t-durable"

    run = MagicMock()
    run.current_state = "running"
    run.id = "pr-old"

    with patch(
        "app.task_registry.spawn.workflow_is_running",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.task_registry.spawn.ensure_process_run",
        new=AsyncMock(return_value="pr-new"),
    ) as mock_ensure, patch(
        "app.task_registry.spawn.signal_spawn_leg",
        new=AsyncMock(return_value=True),
    ) as mock_signal:
        out = await spawn_process_for_task(
            "t-durable",
            leg_intent="leg two",
            db=_mock_session(task, run),
        )

    assert out.get("spawned") is True
    assert out.get("spawn_leg_signaled") is True
    assert out.get("workflow_started") is False
    mock_ensure.assert_awaited_once()
    assert mock_ensure.await_args[0][0].get("durable_leg") is True
    assert mock_ensure.await_args[0][0].get("actor") == "spawn"
    mock_signal.assert_awaited_once_with("t-durable", "pr-new", "leg two")


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'spawn.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as db:
        db.add_all([Task(id="t2", goal="resume work", task_kind="durable", task_type="user", status="completed",
                         openclaw_session_key="agent:main:main", correlation_id="t2"),
                    ProcessRun(id="pr-done", task_id="t2", current_state="completed")])
        await db.commit()
    yield sessions
    await engine.dispose()


@pytest.mark.asyncio
async def test_spawn_refuses_a_finished_task(factory):
    """A finished task is never reopened: no run, no workflow, and its status stays (CI-13)."""
    from app.task_registry.spawn import spawn_process_for_task

    with patch(
        "app.task_registry.spawn.workflow_is_running",
        new=AsyncMock(return_value=False),
    ), patch(
        "app.task_registry.spawn.ensure_process_run",
        new=AsyncMock(return_value="pr-next"),
    ) as mock_ensure, patch(
        "app.task_registry.spawn.start_task_workflow",
        new=AsyncMock(),
    ) as mock_start:
        async with factory() as db:
            out = await spawn_process_for_task("t2", db=db)
            await db.commit()

    assert out == {"task_id": "t2", "spawned": False, "reason": "task_terminal"}
    mock_ensure.assert_not_awaited()
    mock_start.assert_not_awaited()
    async with factory() as db:
        assert (await db.get(Task, "t2")).status == "completed"
        assert [r.id for r in (await db.execute(select(ProcessRun))).scalars()] == ["pr-done"]


@pytest.mark.asyncio
async def test_spawn_starts_nothing_when_the_move_to_running_is_refused(factory, caplog):
    """A deploying task whose last run ended belongs to its deploy: the refused move makes no run and starts no
    workflow (D-6: side effects only on a change)."""
    import logging

    from app.task_registry.spawn import spawn_process_for_task

    caplog.set_level(logging.WARNING, logger="rmp.transitions")
    async with factory() as db:
        db.add_all([Task(id="t3", goal="ship the fix", task_type="user", status="deploying",
                         openclaw_session_key="agent:main:main", correlation_id="t3"),
                    ProcessRun(id="pr-shipped", task_id="t3", current_state="completed")])
        await db.commit()

    with patch(
        "app.task_registry.spawn.workflow_is_running",
        new=AsyncMock(return_value=False),
    ), patch(
        "app.task_registry.spawn.ensure_process_run",
        new=AsyncMock(return_value="pr-next"),
    ) as mock_ensure, patch(
        "app.task_registry.spawn.start_task_workflow",
        new=AsyncMock(),
    ) as mock_start:
        async with factory() as db:
            out = await spawn_process_for_task("t3", db=db)
            await db.commit()

    assert out == {"task_id": "t3", "spawned": False, "reason": "active_process_exists"}
    mock_ensure.assert_not_awaited()
    mock_start.assert_not_awaited()
    assert [r.getMessage() for r in caplog.records if r.name == "rmp.transitions"] == [
        "task transition refused: t3 deploying -> running actor=spawn cause=spawn_process"]
    async with factory() as db:
        assert (await db.get(Task, "t3")).status == "deploying"


@pytest.mark.asyncio
async def test_the_spawn_route_and_intake_turn_a_finished_task_away(factory, monkeypatch):
    """The route answers 409, and intake makes the message a new task."""
    from app.api import server
    from app.task_registry.intake_handlers import handle_intake_outcome

    async def get_db():
        async with factory() as db:
            yield db

    monkeypatch.setenv("RMP_API_KEY", "spawn-key")
    server.app.dependency_overrides[server.get_db] = get_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://rmp",
                                     headers={"X-RMP-API-Key": "spawn-key"}) as client:
            refused = await client.post("/tasks/t2/spawn_process")
    finally:
        server.app.dependency_overrides.pop(server.get_db, None)
    assert refused.status_code == 409, refused.text

    decision = {"effective_decision": "spawn_process", "decision": "spawn_process", "target_task_id": "t2",
                "intake_mode": "enforce", "request_hash": "abc", "confidence": 90, "rationale": "next leg"}
    with patch("app.task_registry.intake_handlers.record_intake_decision", new=AsyncMock(return_value="dec-1")):
        async with factory() as db:
            out = await handle_intake_outcome(decision, request=None, db=db, intent="resume work",
                                              session_key="agent:main:main", tags=["user-request"])
    assert out is None
    async with factory() as db:
        assert [r.id for r in (await db.execute(select(ProcessRun))).scalars()] == ["pr-done"]
