"""The dashboard names no task status itself: its Cancel and Retry lists and its counters come from the API."""
import re
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api import server
from app.db.models import Base, Task

TEMPLATE = Path(server.__file__).resolve().parent / "templates" / "dashboard.html"


@pytest.fixture
async def api(tmp_path, monkeypatch):
    """The API on a fresh SQLite file, with the dashboard's readiness, memory and scanner probes stood in."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rmp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def get_db():
        async with factory() as s:
            yield s

    monkeypatch.setattr(server, "_read_current_model", lambda: {"provider": "p", "model": "m"})
    monkeypatch.setattr(server.MemoryRouter, "vector_status", AsyncMock(return_value={"enabled": False}))
    monkeypatch.setattr(server, "telemetry_status", lambda: {})
    monkeypatch.setattr(server, "get_scanner_status", AsyncMock(return_value=[]))
    monkeypatch.setattr(server, "run_all_checks", AsyncMock(return_value={}))
    monkeypatch.setattr(server, "get_last_backup_info", lambda: None)
    server.app.dependency_overrides[server.get_db] = get_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://rmp") as client:
            yield client, factory
    finally:
        server.app.dependency_overrides.pop(server.get_db, None)
        await engine.dispose()


async def test_dashboard_data_carries_the_status_sets(api):
    client, factory = api
    async with factory() as db:
        db.add_all([Task(id="a", goal="a", status="running"), Task(id="b", goal="b", status="completed"),
                    Task(id="c", goal="c", status="blocked")])
        await db.commit()

    data = (await client.get("/api/dashboard-data")).json()

    assert data["status_sets"] == {
        "cancellable": ["blocked", "created", "pending_user_input", "running"],
        "retryable": ["cancelled", "compensated", "completed", "failed", "stopped_by_user"],
    }
    assert data["stats"] == {"total": 3, "running": 1, "completed": 1, "failed": 0}


def test_the_dashboard_template_names_no_status():
    template = TEMPLATE.read_text()
    assert "selectattr('status'" not in template
    assert not re.search(r"""['"](pending_user_input|stopped_by_user|compensated)['"]""", template)


async def test_the_dashboard_page_counts_from_the_stats(api):
    client, factory = api
    async with factory() as db:
        db.add_all([Task(id="a", goal="a", status="running"), Task(id="b", goal="b", status="failed"),
                    Task(id="c", goal="c", status="failed")])
        await db.commit()

    page = await client.get("/")

    assert page.status_code == 200, page.text
    assert 'id="stat-running">1<' in page.text
    assert 'id="stat-completed">0<' in page.text
    assert 'id="stat-failed">2<' in page.text
