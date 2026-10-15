"""Session discovery on the API path, as in production: a DM that arrives on agent:main:main belongs to the Slack DM
conversation in OpenClaw's session store, and the main session's active-task lookup finds it there."""
import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app import config, openclaw_sessions
from app.activities import db_activities
from app.api import server
from app.db.models import Base, Task
from app.task_registry.session_identity import MAIN_SESSION

DM = "agent:main:slack:channel:d0seeded"
KEY = "session-identity-key"


@pytest.fixture
def seeded_dm():
    """The Slack DM session in the session store under the suite's temporary OPENCLAW_HOME, which is empty until a
    test seeds it."""
    store = openclaw_sessions.SESSIONS_JSON_PATH
    assert not store.exists()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({MAIN_SESSION: {"sessionId": "main-session"}, DM: {"sessionId": "dm-session"}}))
    yield DM
    store.unlink()


@pytest.fixture
async def api(tmp_path, monkeypatch):
    """The API on a fresh SQLite file with intake off; the workflow start is recorded, not sent to Temporal."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rmp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    started = []

    async def get_db():
        async with factory() as s:
            yield s

    async def start_task_workflow(task_id, intent, session_key, task_type, **kwargs):
        started.append(session_key)

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"task_registry": {"intake_mode": "off"}}))
    monkeypatch.setattr(config, "SETTINGS_PATH", str(settings))
    monkeypatch.setenv("RMP_API_KEY", KEY)
    monkeypatch.setattr(db_activities, "AsyncSessionLocal", factory)
    monkeypatch.setattr(server, "_start_task_workflow", start_task_workflow)
    server.app.dependency_overrides[server.get_db] = get_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://rmp",
                                     headers={"X-RMP-API-Key": KEY}) as client:
            yield client, factory, started
    finally:
        server.app.dependency_overrides.pop(server.get_db, None)
        await engine.dispose()


async def test_a_dm_on_the_main_session_is_stored_under_the_slack_dm_and_main_finds_it(api, seeded_dm):
    client, factory, started = api
    created = await client.post("/tasks", json={"intent": "What is on my calendar today?",
                                                "session_key": MAIN_SESSION, "tags": ["user-request"]})
    assert created.status_code == 200, created.text
    task_id = created.json()["task_id"]
    async with factory() as s:
        stored = (await s.execute(select(Task.openclaw_session_key).where(Task.id == task_id))).scalar_one()
    assert stored == seeded_dm and started == [seeded_dm]

    active = (await client.get(f"/sessions/{MAIN_SESSION}/active_task")).json()["active_task"]
    assert active == {"id": task_id, "status": "created", "goal": "What is on my calendar today?"}
