"""Create task intake integration (mocked LLM)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_create_task_uses_intake_runner():
    from app.task_registry.intake_runner import run_classify_task_intake

    payload = {
        "intent": "hello",
        "session_key": "agent:main:main",
        "tags": ["user-request"],
        "recurrence_key": None,
        "task_type": "user",
    }
    mock_client = MagicMock()
    mock_client.execute_workflow = AsyncMock(
        return_value={
            "decision": "create_fresh",
            "effective_decision": "create_fresh",
            "intake_mode": "enforce",
            "confidence": 80,
            "request_hash": "x",
        }
    )
    mock_det = AsyncMock()
    mock_inline = AsyncMock()
    with patch(
        "app.task_registry.intake_runner.connect_temporal",
        new=AsyncMock(return_value=mock_client),
    ):
        with patch(
            "app.activities.intake_activities.classify_task_intake",
            mock_inline,
        ):
            with patch(
                "app.activities.intake_activities.classify_task_intake_deterministic",
                mock_det,
            ):
                result = await run_classify_task_intake(payload)
    assert result["effective_decision"] == "create_fresh"
    mock_client.execute_workflow.assert_awaited_once()
    mock_inline.assert_not_awaited()
    mock_det.assert_not_awaited()


async def test_attach_fallback_never_passes_through_cancelled(tmp_path, monkeypatch):
    """Intake attaches the message to a task whose workflow is gone: the message's own reservation becomes its task,
    staying created throughout; it is never cancelled and then reopened."""
    import json

    import httpx
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app import config
    from app.activities import db_activities
    from app.api import server
    from app.db.models import Base, Task

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rmp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    writes = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE TASKS"):
            writes.append(parameters)

    async def get_db():
        async with factory() as s:
            yield s

    started = []

    async def start_task_workflow(task_id, *args, **kwargs):
        started.append(task_id)

    handle = MagicMock()
    handle.signal = AsyncMock(side_effect=RuntimeError("workflow not found"))
    client = MagicMock()
    client.get_workflow_handle.return_value = handle
    outcome = {"intake_action": "attach_active", "task_id": "live-1", "signal_required": True,
               "signals": [{"task_id": "live-1", "signal_text": "also check the logs"}],
               "_guided_memory_block": "PROCESS BRIEF: continue task live-1", "intake_decision_id": "dec-1"}
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"task_registry": {"intake_mode": "enforce"}}))
    monkeypatch.setattr(config, "SETTINGS_PATH", str(settings))
    monkeypatch.setenv("RMP_API_KEY", "attach-key")
    monkeypatch.setattr(db_activities, "AsyncSessionLocal", factory)
    monkeypatch.setattr(server, "_start_task_workflow", start_task_workflow)
    monkeypatch.setattr(server, "connect_temporal", AsyncMock(return_value=client))
    monkeypatch.setattr("app.task_registry.intake_runner.run_classify_task_intake",
                        AsyncMock(return_value={"decision": "attach_active", "effective_decision": "attach_active"}))
    monkeypatch.setattr("app.task_registry.intake_handlers.handle_intake_outcome", AsyncMock(return_value=outcome))
    server.app.dependency_overrides[server.get_db] = get_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://rmp",
                                     headers={"X-RMP-API-Key": "attach-key"}) as http:
            created = await http.post("/tasks", json={"intent": "also check the logs", "tags": ["user-request"],
                                                      "session_key": "agent:main:slack:channel:d0attach"})
        async with factory() as db:
            task = await db.get(Task, created.json()["task_id"])
    finally:
        server.app.dependency_overrides.pop(server.get_db, None)
        await engine.dispose()

    assert created.status_code == 200, created.text
    assert started == [task.id] and task.status == "created"
    assert "closed_reason" not in (task.supplementary_context or {})
    assert not [params for params in writes if "cancelled" in (params or ())]


@pytest.mark.parametrize("status", ["created", "pending_user_input", "running"])
async def test_a_stale_reservation_is_reused_only_while_it_is_still_created(tmp_path, monkeypatch, caplog, status):
    """A repeat of a message whose intake stalled continues its reservation. A reservation that moved on meanwhile is
    not reused: 409, and the row stays as it was, a move the writer made in the request rolled back with it."""
    import json
    import logging
    from datetime import datetime, timedelta, timezone

    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app import config
    from app.activities import db_activities
    from app.api import server
    from app.db.models import Base, Task

    caplog.set_level(logging.WARNING, logger="rmp.transitions")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rmp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    stalled = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=server.INTAKE_RESERVATION_WINDOW_SEC + 60)
    async with factory() as db:
        db.add(Task(id="reserved", idempotency_key="idem-reuse", goal="check the logs", task_type="user",
                    status=status, supplementary_context={"intake_reserved": True}, updated_at=stalled))
        await db.commit()

    async def get_db():
        async with factory() as s:
            yield s

    started = []

    async def start_task_workflow(task_id, *args, **kwargs):
        started.append(task_id)

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"task_registry": {"intake_mode": "off"}}))
    monkeypatch.setattr(config, "SETTINGS_PATH", str(settings))
    monkeypatch.setenv("RMP_API_KEY", "reuse-key")
    monkeypatch.setattr(db_activities, "AsyncSessionLocal", factory)
    monkeypatch.setattr(server, "_start_task_workflow", start_task_workflow)
    server.app.dependency_overrides[server.get_db] = get_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://rmp",
                                     headers={"X-RMP-API-Key": "reuse-key"}) as http:
            response = await http.post("/tasks", json={"intent": "check the logs again", "tags": ["user-request"],
                                                       "idempotency_key": "idem-reuse",
                                                       "session_key": "agent:main:slack:channel:d0reuse"})
        async with factory() as db:
            task = await db.get(Task, "reserved")
    finally:
        server.app.dependency_overrides.pop(server.get_db, None)
        await engine.dispose()

    refused = [r.getMessage() for r in caplog.records if r.name == "rmp.transitions"]
    if status == "created":
        assert response.status_code == 200, response.text
        assert response.json()["task_id"] == "reserved" and started == ["reserved"]
        assert task.status == "created" and task.goal == "check the logs again" and refused == []
        return
    assert response.status_code == 409, response.text
    assert started == []
    assert task.status == status and task.goal == "check the logs"
    assert task.supplementary_context == {"intake_reserved": True}
    # pending_user_input may move to created, so the writer moved it; running may not, so it refused.
    assert refused == ([] if status == "pending_user_input" else [
        "task transition refused: reserved running -> created actor=api:/tasks cause=reservation reused"])
