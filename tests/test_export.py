import os
import tempfile
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Event, Task


@pytest.mark.asyncio
async def test_task_export_bundle(monkeypatch):
    db_path = os.path.join(tempfile.mkdtemp(), "export_test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    import app.db.database as db_mod

    engine = create_async_engine(db_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", session_factory)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    task_id = str(uuid.uuid4())
    async with session_factory() as db:
        db.add(
            Task(
                id=task_id,
                correlation_id=task_id,
                goal="export test",
                status="completed",
            )
        )
        db.add(
            Event(
                correlation_id=task_id,
                entity_type="task",
                entity_id=task_id,
                event_type="task.created",
                event_payload={"test": True},
            )
        )
        await db.commit()

    from app.api.server import app, get_db

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/tasks/{task_id}/export")

    app.dependency_overrides.clear()
    await engine.dispose()

    assert resp.status_code == 200
    body = resp.json()
    assert body["task"]["id"] == task_id
    assert len(body["events"]) >= 1
    assert "observations" in body
    assert "artifacts" in body
