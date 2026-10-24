"""The task routes read statuses through app.status: GET /tasks/{id} says whether a status is terminal, /retry runs
only a finished task, and suspend-all cancels every task whose workflow it terminates."""
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api import server
from app.db.models import Base, Task

KEY = "task-status-key"


@pytest.fixture
async def api(tmp_path, monkeypatch):
    """The API on a fresh SQLite file; workflow starts are recorded, not sent to Temporal."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rmp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    started = []

    async def get_db():
        async with factory() as s:
            yield s

    async def start_task_workflow(task_id, *args, **kwargs):
        started.append(task_id)

    monkeypatch.setenv("RMP_API_KEY", KEY)
    monkeypatch.setattr(server, "_start_task_workflow", start_task_workflow)
    server.app.dependency_overrides[server.get_db] = get_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://rmp",
                                     headers={"X-RMP-API-Key": KEY}) as client:
            yield client, factory, started
    finally:
        server.app.dependency_overrides.pop(server.get_db, None)
        await engine.dispose()


async def seed(factory, **statuses):
    async with factory() as db:
        db.add_all([Task(id=tid, goal=f"goal {tid}", task_type="coding", status=status)
                    for tid, status in statuses.items()])
        await db.commit()


async def stored(factory, tid):
    async with factory() as db:
        return (await db.execute(select(Task.status).where(Task.id == tid))).scalar_one()


async def test_task_status_says_terminal(api):
    client, factory, _ = api
    await seed(factory, done="compensated", live="running")

    done = (await client.get("/tasks/done")).json()
    live = (await client.get("/tasks/live")).json()

    assert done["status"] == "compensated" and done["terminal"] is True
    assert live["status"] == "running" and live["terminal"] is False


async def test_retry_refuses_a_task_that_is_not_finished(api):
    client, factory, started = api
    await seed(factory, queued="blocked", shipping="deploying", done="compensated")

    for tid in ("queued", "shipping"):
        refused = await client.post(f"/tasks/{tid}/retry")
        assert refused.status_code == 409, refused.text
    assert started == []

    retried = await client.post("/tasks/done/retry")
    assert retried.status_code == 200, retried.text
    assert started == [retried.json()["task_id"]]


async def test_suspend_all_cancels_a_queued_coding_task(api, monkeypatch):
    client, factory, _ = api
    await seed(factory, queued="blocked", asking="pending_user_input", shipping="deploying", done="completed")

    class NoWorkflows:
        def list_workflows(self, query):
            async def none():
                return
                yield
            return none()

    async def connect():
        return NoWorkflows()

    aborted = []
    monkeypatch.setattr(server, "connect_temporal", connect)
    monkeypatch.setattr("app.openclaw_control.schedule_abort", lambda task_id, *, reason: aborted.append(task_id))
    monkeypatch.setattr(server, "update_stored_settings", lambda apply: None)

    out = await client.post("/dev/suspend-all")

    assert out.status_code == 200, out.text
    assert out.json()["stopped_tasks"] == 2
    assert sorted(aborted) == ["asking", "queued"]
    assert await stored(factory, "queued") == "cancelled"
    assert await stored(factory, "asking") == "cancelled"
    # The self-deploy owns a deploying task, and a finished task stays as it ended.
    assert await stored(factory, "shipping") == "deploying"
    assert await stored(factory, "done") == "completed"
