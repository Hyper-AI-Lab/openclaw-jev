"""Session discovery on the API path, as in production: a DM that arrives on agent:main:main belongs to the Slack DM
conversation in OpenClaw's session store, not to a Slack channel there, and the main session's active-task lookups, the
API's and intake's, find it."""
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app import config, openclaw_sessions
from app.activities import db_activities
from app.api import server
from app.db.models import Base, Task
from app.task_registry import retriever
from app.task_registry.session_identity import MAIN_SESSION
from tests.openclaw97 import Store97

REPO = Path(__file__).resolve().parents[1]
DM = "agent:main:slack:channel:d0seeded"
CHANNEL = "agent:main:slack:channel:c0seeded"
KEY = "session-identity-key"


@pytest.fixture
def seeded_dm():
    """The Slack DM session, next to a Slack channel that sorts before it, in OpenClaw's SQLite agent store, the format
    production reads (it has no sessions.json), under the suite's temporary OPENCLAW_HOME, which is empty until a test
    seeds it."""
    db = openclaw_sessions.AGENT_DB_PATH
    assert not openclaw_sessions.SESSIONS_JSON_PATH.exists() and not db.exists()
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        store = Store97(db)
        store.session(MAIN_SESSION, "main-session")
        store.session(CHANNEL, "channel-session")
        store.session(DM, "dm-session")
        store.con.close()
        yield DM
    finally:
        db.unlink(missing_ok=True)


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
    assert (await client.get(f"/sessions/{MAIN_SESSION}/active_user_task")).json()["active_task"] == active
    async with factory() as s:
        snapshot = await retriever.fetch_active_tasks(session_key=MAIN_SESSION, db=s)
    assert [row["task_id"] for row in snapshot] == [task_id]


def test_no_app_module_reads_pytest_current_test():
    """Production code runs the same under pytest: no module under app/ asks whether a test is running."""
    modules = sorted(REPO.glob("app/**/*.py"))
    assert REPO / "app" / "api" / "server.py" in modules
    readers = [path.relative_to(REPO).as_posix() for path in modules
               if "PYTEST_CURRENT_TEST" in path.read_text(encoding="utf-8")]
    assert readers == []
