"""ops/temporal_purge_running.py: the purge ends the Running workflows of finished tasks and keeps those of running
ones; a forced recovery ends every one and fails the tasks it leaves unfinished.

The script is loaded as the operator runs it; its Temporal client is a fake and its database a SQLite file in tmp_path.
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Task

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def purge(tmp_path, monkeypatch):
    """The loaded script, with a completed task t-done and a running task t-live, each with a Running workflow."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'purge.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Task.__table__])
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add_all([Task(id="t-done", goal="g", status="completed"), Task(id="t-live", goal="g", status="running")])
        await db.commit()

    script = ROOT / "ops" / "temporal_purge_running.py"
    module_spec = importlib.util.spec_from_file_location("temporal_purge_running", script)
    module = importlib.util.module_from_spec(module_spec)
    # The script puts the live checkout first on sys.path; the test's own path comes back as soon as it is loaded.
    path = list(sys.path)
    try:
        module_spec.loader.exec_module(module)
    finally:
        sys.path[:] = path

    async def running(query):
        for workflow_id in ("workflow-t-done", "workflow-t-live"):
            yield SimpleNamespace(id=workflow_id, start_time=None)

    client = MagicMock()
    client.list_workflows = running
    client.get_workflow_handle.return_value.terminate = AsyncMock()
    monkeypatch.setattr(module, "Client", MagicMock(connect=AsyncMock(return_value=client)))
    monkeypatch.setattr(module, "AsyncSessionLocal", factory)

    async def statuses():
        async with factory() as db:
            return dict((await db.execute(select(Task.id, Task.status))).all())

    yield SimpleNamespace(run=module.purge_running, client=client, statuses=statuses)
    await engine.dispose()


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "terminate"])
async def test_the_purge_ends_the_workflow_of_a_finished_task_and_keeps_a_running_one(purge, dry_run):
    stats = await purge.run(dry_run=dry_run)

    assert stats == {"scanned": 2, "terminated": 1, "skipped": 1, "errors": 0, "tasks_failed": 0}
    terminate = purge.client.get_workflow_handle.return_value.terminate
    if dry_run:
        terminate.assert_not_awaited()
    else:
        purge.client.get_workflow_handle.assert_called_once_with("workflow-t-done")
        terminate.assert_awaited_once()
    assert await purge.statuses() == {"t-done": "completed", "t-live": "running"}


async def test_a_forced_recovery_ends_every_workflow_and_fails_the_unfinished_tasks(purge):
    stats = await purge.run(force_recovery=True)

    assert stats == {"scanned": 2, "terminated": 2, "skipped": 0, "errors": 0, "tasks_failed": 1}
    handles = [c.args[0] for c in purge.client.get_workflow_handle.call_args_list]
    assert handles == ["workflow-t-done", "workflow-t-live"]
    assert await purge.statuses() == {"t-done": "completed", "t-live": "failed"}
