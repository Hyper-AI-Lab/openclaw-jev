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
