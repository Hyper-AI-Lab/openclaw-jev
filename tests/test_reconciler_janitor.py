"""Reconciler and workflow janitor tests (W5)."""
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.reconciler import (
    STALE_TASK_MINUTES,
    STUCK_REPAIR_MINUTES,
    _cleanup_orphan_plan_children,
    _task_is_internal,
    count_stuck_running_workflows,
)


def test_stale_threshold_tuned():
    assert STALE_TASK_MINUTES == 20
    assert STUCK_REPAIR_MINUTES == 45


def test_internal_task_skips_notify_spam():
    class Task:
        goal = "RMP CANARY test"
        task_type = "canary"

    assert _task_is_internal(Task) is True

    class SmokeTask:
        goal = "Intake attach smoke 20260608: research topic"
        task_type = "user"

    assert _task_is_internal(SmokeTask) is True


@pytest.mark.asyncio
async def test_cleanup_orphan_plan_children():
    client = MagicMock()
    running_wf = MagicMock()
    running_wf.id = "task-1-plan-step-1"
    running_wf.status = __import__(
        "temporalio.client", fromlist=["WorkflowExecutionStatus"]
    ).WorkflowExecutionStatus.RUNNING

    async def fake_list(*args, **kwargs):
        yield running_wf

    client.list_workflows = fake_list
    client.get_workflow_handle.return_value.terminate = AsyncMock()

    with patch("app.reconciler._terminate_workflow", new_callable=AsyncMock) as term:
        term.return_value = True
        count = await _cleanup_orphan_plan_children(client, "task-1")
    assert count == 1


@pytest.mark.asyncio
async def test_a_running_deep_recall_of_a_terminal_task_is_cleaned_up():
    from types import SimpleNamespace

    from temporalio.client import WorkflowExecutionStatus

    async def no_plan_children(*args, **kwargs):
        return
        yield

    client = MagicMock()
    client.list_workflows = no_plan_children
    client.get_workflow_handle.return_value.describe = AsyncMock(
        return_value=SimpleNamespace(status=WorkflowExecutionStatus.RUNNING)
    )
    with patch("app.reconciler._terminate_workflow", new_callable=AsyncMock, return_value=True) as term:
        assert await _cleanup_orphan_plan_children(client, "task-1") == 1
    assert term.await_args.args[1] == "task-1-recall"


@pytest.mark.asyncio
async def test_the_janitor_reads_the_task_of_a_deep_recall(monkeypatch):
    import importlib.util
    from datetime import timezone

    from app.config import RMP_ROOT

    spec = importlib.util.spec_from_file_location("workflow_janitor", os.path.join(RMP_ROOT, "ops", "workflow_janitor.py"))
    janitor_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(janitor_mod)
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    live, done = MagicMock(id="t-live-recall", start_time=old), MagicMock(id="t-done-recall", start_time=old)

    async def fake_list(*args, **kwargs):
        yield live
        yield done

    client = MagicMock()
    client.list_workflows = fake_list
    client.get_workflow_handle.return_value.terminate = AsyncMock()
    looked_up = []

    async def execute(query):
        task_id = query.compile().params["id_1"]
        looked_up.append(task_id)
        result = MagicMock()
        result.scalar_one_or_none.return_value = MagicMock(status="running" if task_id == "t-live" else "completed")
        return result

    with patch.object(janitor_mod, "Client") as mock_client_cls:
        mock_client_cls.connect = AsyncMock(return_value=client)
        with patch.object(janitor_mod, "AsyncSessionLocal") as mock_session:
            db = AsyncMock()
            db.execute = execute
            mock_session.return_value.__aenter__.return_value = db
            stats = await janitor_mod.janitor_once(max_age_hours=24)
    assert looked_up == ["t-live", "t-done"] and stats["terminated"] == 1
    client.get_workflow_handle.assert_called_with("t-done-recall")


@pytest.mark.asyncio
async def test_the_janitor_keeps_a_coding_task_waiting_days_for_approval():
    import importlib.util
    from datetime import timezone

    from app.config import RMP_ROOT

    spec = importlib.util.spec_from_file_location("workflow_janitor", os.path.join(RMP_ROOT, "ops", "workflow_janitor.py"))
    janitor_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(janitor_mod)
    gate = MagicMock(id="workflow-t-gate", start_time=datetime.now(timezone.utc) - timedelta(days=3))

    async def fake_list(*args, **kwargs):
        yield gate

    client = MagicMock()
    client.list_workflows = fake_list
    client.get_workflow_handle.return_value.terminate = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = MagicMock(status="pending_user_input")

    with patch.object(janitor_mod, "Client") as mock_client_cls:
        mock_client_cls.connect = AsyncMock(return_value=client)
        with patch.object(janitor_mod, "AsyncSessionLocal") as mock_session:
            db = AsyncMock()
            db.execute = AsyncMock(return_value=result)
            mock_session.return_value.__aenter__.return_value = db
            stats = await janitor_mod.janitor_once(max_age_hours=24)
    assert stats == {"scanned": 1, "terminated": 0, "errors": 0}
    client.get_workflow_handle.return_value.terminate.assert_not_awaited()


@pytest.mark.asyncio
async def test_count_stuck_created_task_workflows():
    wf = MagicMock()
    wf.id = "workflow-stuck-created"
    wf.status = MagicMock()

    async def fake_list(*args, **kwargs):
        yield wf

    client = MagicMock()
    client.list_workflows = fake_list

    class FakeTask:
        id = "stuck-created"
        status = "created"
        updated_at = datetime.utcnow() - timedelta(minutes=50)

    with patch("app.reconciler._get_temporal", new_callable=AsyncMock, return_value=client):
        with patch("app.reconciler.AsyncSessionLocal") as mock_session:
            db = AsyncMock()
            mock_session.return_value.__aenter__.return_value = db
            result = MagicMock()
            result.scalar_one_or_none.return_value = FakeTask()
            db.execute = AsyncMock(return_value=result)
            count = await count_stuck_running_workflows()
    assert count == 1


@pytest.mark.asyncio
async def test_count_stuck_running_workflows():
    wf = MagicMock()
    wf.id = "workflow-stuck-task"
    wf.status = MagicMock()

    async def fake_list(*args, **kwargs):
        yield wf

    client = MagicMock()
    client.list_workflows = fake_list

    class FakeTask:
        id = "stuck-task"
        status = "running"
        updated_at = datetime.utcnow() - timedelta(minutes=50)

    with patch("app.reconciler._get_temporal", new_callable=AsyncMock, return_value=client):
        with patch("app.reconciler.AsyncSessionLocal") as mock_session:
            db = AsyncMock()
            mock_session.return_value.__aenter__.return_value = db
            result = MagicMock()
            result.scalar_one_or_none.return_value = FakeTask()
            db.execute = AsyncMock(return_value=result)
            count = await count_stuck_running_workflows()
    assert count == 1


@pytest.mark.asyncio
async def test_a_deep_recall_counts_as_stuck_only_once_its_task_has_ended():
    async def fake_list(*args, **kwargs):
        yield MagicMock(id="t-done-recall")
        yield MagicMock(id="t-busy-recall")

    client = MagicMock()
    client.list_workflows = fake_list
    stale = datetime.utcnow() - timedelta(minutes=50)

    async def execute(query):
        task_id = query.compile().params["id_1"]
        result = MagicMock()
        result.scalar_one_or_none.return_value = MagicMock(
            status="completed" if task_id == "t-done" else "running", updated_at=stale
        )
        return result

    with patch("app.reconciler._get_temporal", new_callable=AsyncMock, return_value=client):
        with patch("app.reconciler.AsyncSessionLocal") as mock_session:
            db = AsyncMock()
            db.execute = execute
            mock_session.return_value.__aenter__.return_value = db
            assert await count_stuck_running_workflows() == 1


@pytest.mark.asyncio
async def test_janitor_terminates_old_orphan():
    import importlib.util
    from datetime import timezone

    from app.config import RMP_ROOT

    janitor_path = os.path.join(RMP_ROOT, "ops", "workflow_janitor.py")
    spec = importlib.util.spec_from_file_location(
        "workflow_janitor", janitor_path
    )
    janitor_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(janitor_mod)

    wf = MagicMock()
    wf.id = "workflow-missing-task"
    wf.start_time = datetime.now(timezone.utc) - timedelta(hours=30)

    async def fake_list(*args, **kwargs):
        yield wf

    client = MagicMock()
    client.list_workflows = fake_list
    client.get_workflow_handle.return_value.terminate = AsyncMock()

    with patch.object(janitor_mod, "Client") as mock_client_cls:
        mock_client_cls.connect = AsyncMock(return_value=client)
        with patch.object(janitor_mod, "AsyncSessionLocal") as mock_session:
            db = AsyncMock()
            mock_session.return_value.__aenter__.return_value = db
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            db.execute = AsyncMock(return_value=result)
            stats = await janitor_mod.janitor_once(max_age_hours=24)
    assert stats["terminated"] == 1


@pytest.fixture
async def stale_db(tmp_path):
    """A SQLite file for the reconciler's pass."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base, Event, ProcessRun, Step, Task

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all,
                            tables=[Task.__table__, ProcessRun.__table__, Step.__table__, Event.__table__])
    yield sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _stale_task(task_id, status, age_min):
    from datetime import timezone

    from app.db.models import Task

    return Task(id=task_id, goal="Summarize my inbox", task_type="user", status=status, correlation_id=task_id,
                openclaw_session_key="agent:main:slack:channel:d0test",
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=age_min))


@pytest.mark.asyncio
async def test_reconciler_indexes_the_tasks_it_completes_after_commit(stale_db):
    from temporalio.client import WorkflowExecutionStatus

    from app import reconciler
    from app.db.models import Task

    async with stale_db() as db:
        db.add_all([_stale_task("orphan-reply", "running", 3), _stale_task("stale-done", "created", 25)])
        await db.commit()

    async def recover(client, db, task, now_, stats):
        return "failed" if task.id == "orphan-reply" else None

    order = MagicMock()

    def session():
        """The pass's session, its commits recorded in order with the registry index."""
        s = stale_db()
        commit = s.commit

        async def recorded():
            await commit()
            order.commit()

        s.commit = recorded
        return s

    desc = MagicMock(status=WorkflowExecutionStatus.COMPLETED)
    client = MagicMock()
    client.get_workflow_handle.return_value.describe = AsyncMock(return_value=desc)
    index = AsyncMock(side_effect=lambda task_id: order.index(task_id))

    with patch("app.config.is_development_mode", return_value=False), \
         patch.object(reconciler, "reap_stale_llm_slots_sync", return_value=[]), \
         patch.object(reconciler, "_get_temporal", new_callable=AsyncMock, return_value=client), \
         patch.object(reconciler, "AsyncSessionLocal", session), \
         patch.object(reconciler, "_recover_orphaned_session_reply", side_effect=recover), \
         patch.object(reconciler, "_cleanup_orphan_plan_children", new_callable=AsyncMock, return_value=0), \
         patch.object(reconciler, "close_runs_of_ended_tasks", new_callable=AsyncMock, return_value=0), \
         patch.object(reconciler, "_notify_repair", new_callable=AsyncMock), \
         patch("app.task_registry.hooks.index_terminal_task_async", index):
        stats = await reconciler.reconcile_once()

    async with stale_db() as db:
        assert (await db.get(Task, "stale-done")).status == "completed"
    assert stats["registry_indexed"] == 2
    assert [c[0] for c in order.mock_calls] == ["commit", "index", "index"]
    assert [c.args[0] for c in index.await_args_list] == ["orphan-reply", "stale-done"]


@pytest.mark.parametrize("workflow_status", ["COMPLETED", "TERMINATED", "FAILED"])
async def test_stale_repair_does_not_overwrite_a_terminal_task(stale_db, caplog, workflow_status):
    """The task ended (here by /cancel) while the pass looked at its workflow: the repair's write is refused, and the
    pass records, notifies and indexes nothing for it, and leaves its runs to the run close."""
    import logging

    from sqlalchemy import select
    from temporalio.client import WorkflowExecutionStatus

    from app import reconciler
    from app.db.models import Event, ProcessRun, Task

    caplog.set_level(logging.WARNING, logger="rmp.transitions")
    async with stale_db() as db:
        db.add_all([_stale_task("stale", "created", 25),
                    ProcessRun(id="run-open", task_id="stale", current_state="running")])
        await db.commit()

    async def cancelled_meanwhile():
        async with stale_db() as db:
            (await db.get(Task, "stale")).status = "cancelled"
            await db.commit()
        return MagicMock(status=WorkflowExecutionStatus[workflow_status])

    client = MagicMock()
    client.get_workflow_handle.return_value.describe = AsyncMock(side_effect=cancelled_meanwhile)
    client.get_workflow_handle.return_value.signal = AsyncMock()

    with patch("app.config.is_development_mode", return_value=False), \
         patch.object(reconciler, "reap_stale_llm_slots_sync", return_value=[]), \
         patch.object(reconciler, "_get_temporal", new_callable=AsyncMock, return_value=client), \
         patch.object(reconciler, "AsyncSessionLocal", stale_db), \
         patch.object(reconciler, "_close_orphaned_coding_tasks", new_callable=AsyncMock, return_value=[]), \
         patch.object(reconciler, "_end_claude_sessions_of_finished_tasks", AsyncMock(return_value=0)), \
         patch.object(reconciler, "_recover_orphaned_session_reply", new_callable=AsyncMock, return_value=None), \
         patch.object(reconciler, "_cleanup_orphan_plan_children", new_callable=AsyncMock, return_value=0) as cleanup, \
         patch.object(reconciler, "close_runs_of_ended_tasks", new_callable=AsyncMock, return_value=0), \
         patch.object(reconciler, "_notify_repair", new_callable=AsyncMock) as notify, \
         patch("app.task_registry.hooks.index_terminal_task_async", new_callable=AsyncMock) as index:
        stats = await reconciler.reconcile_once()

    async with stale_db() as db:
        assert (await db.get(Task, "stale")).status == "cancelled"
        assert (await db.get(ProcessRun, "run-open")).current_state == "running"
        assert (await db.execute(select(Event))).scalars().all() == []
    assert stats["repaired"] == 0 and stats["registry_indexed"] == 0
    notify.assert_not_awaited()
    cleanup.assert_not_awaited()
    index.assert_not_awaited()
    to = "completed" if workflow_status == "COMPLETED" else "cancelled" if workflow_status == "TERMINATED" else "failed"
    cause = "workflow completed" if workflow_status == "COMPLETED" else f"workflow {workflow_status}"
    refused = [r.getMessage() for r in caplog.records if r.name == "rmp.transitions"]
    if to == "cancelled":
        assert refused == []
    else:
        assert refused == [f"task transition refused: stale cancelled -> {to} actor=reconciler cause={cause}"]


@pytest.mark.parametrize("run_state, to", [("awaiting_approval", "compensated"), ("running", "failed")])
async def test_a_stuck_task_that_finished_meanwhile_is_left_as_it_ended(stale_db, caplog, run_state, to):
    """The workflow's own close lands while the stuck repair terminates it: the repair's compensation (a run that made
    progress) or failure is refused, and the repair counts, records and reports nothing."""
    import logging

    from sqlalchemy import select
    from temporalio.client import WorkflowExecutionStatus

    from app import reconciler
    from app.activities import db_activities
    from app.db.models import Event, ProcessRun, Task

    caplog.set_level(logging.WARNING, logger="rmp.transitions")
    async with stale_db() as db:
        db.add_all([_stale_task("stuck", "running", STUCK_REPAIR_MINUTES + 15),
                    ProcessRun(id="run-1", task_id="stuck", process_type="generic_task", current_state=run_state)])
        await db.commit()

    async def completed_meanwhile():
        async with stale_db() as db:
            (await db.get(Task, "stuck")).status = "completed"
            await db.commit()
        return MagicMock(status=WorkflowExecutionStatus.RUNNING)

    client = MagicMock()
    client.get_workflow_handle.return_value.describe = AsyncMock(side_effect=completed_meanwhile)
    client.get_workflow_handle.return_value.terminate = AsyncMock()
    stats = {"repaired": 0, "events": 0}

    with patch.object(db_activities, "AsyncSessionLocal", stale_db), \
         patch("app.memory.router.MemoryRouter.write", new_callable=AsyncMock), \
         patch("app.task_registry.hooks.index_terminal_task_async", new_callable=AsyncMock), \
         patch.object(reconciler, "_cleanup_orphan_plan_children", new_callable=AsyncMock, return_value=0), \
         patch.object(reconciler, "_notify_repair", new_callable=AsyncMock) as notify, \
         patch.object(reconciler, "metrics_inc") as metric:
        async with stale_db() as db:
            task = await db.get(Task, "stuck")
            repaired = await reconciler._repair_stuck_running_task(client, db, task, datetime.now(timezone.utc).replace(tzinfo=None), stats)
            await db.commit()

    async with stale_db() as db:
        assert (await db.get(Task, "stuck")).status == "completed"
        assert (await db.execute(select(Event))).scalars().all() == []
    assert repaired is False and stats == {"repaired": 0, "events": 0, "terminated": 1, "orphans_terminated": 0}
    client.get_workflow_handle.return_value.terminate.assert_awaited_once()
    notify.assert_not_awaited()
    metric.assert_not_called()
    assert [r.getMessage() for r in caplog.records if r.name == "rmp.transitions"] == [
        f"task transition refused: stuck completed -> {to} actor=reconciler cause=stuck >{STUCK_REPAIR_MINUTES}m"]


async def test_a_cancelled_run_of_a_completed_workflow_stays_cancelled(tmp_path):
    """The stale repair completes the task of a completed workflow and closes its open runs; a run that already ended
    (here canceled) keeps its state."""
    from datetime import timezone

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from temporalio.client import WorkflowExecutionStatus

    from app import reconciler
    from app.db.models import Base, Event, ProcessRun, Task

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Task.__table__, ProcessRun.__table__, Event.__table__])
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=STALE_TASK_MINUTES + 5)
    async with factory() as db:
        db.add_all([
            Task(id="stale-done", goal="Summarize my inbox", task_type="user", status="created", updated_at=stale),
            ProcessRun(id="run-canceled", task_id="stale-done", current_state="canceled"),
            ProcessRun(id="run-open", task_id="stale-done", current_state="running"),
        ])
        await db.commit()
    client = MagicMock()
    client.get_workflow_handle.return_value.describe = AsyncMock(
        return_value=MagicMock(status=WorkflowExecutionStatus.COMPLETED))

    try:
        with patch("app.config.is_development_mode", return_value=False), \
             patch.object(reconciler, "reap_stale_llm_slots_sync", return_value=[]), \
             patch.object(reconciler, "_get_temporal", new_callable=AsyncMock, return_value=client), \
             patch.object(reconciler, "AsyncSessionLocal", factory), \
             patch.object(reconciler, "_close_orphaned_coding_tasks", new_callable=AsyncMock, return_value=[]), \
             patch.object(reconciler, "_end_claude_sessions_of_finished_tasks", AsyncMock(return_value=0)), \
             patch.object(reconciler, "_recover_orphaned_session_reply", new_callable=AsyncMock, return_value=None), \
             patch.object(reconciler, "_cleanup_orphan_plan_children", new_callable=AsyncMock, return_value=0), \
             patch.object(reconciler, "_notify_repair", new_callable=AsyncMock), \
             patch("app.task_registry.hooks.index_terminal_task_async", new_callable=AsyncMock):
            stats = await reconciler.reconcile_once()

        async with factory() as db:
            status = (await db.execute(select(Task.status).where(Task.id == "stale-done"))).scalar_one()
            runs = dict((await db.execute(select(ProcessRun.id, ProcessRun.current_state))).all())
    finally:
        await engine.dispose()

    assert stats["repaired"] == 1 and status == "completed"
    assert runs == {"run-canceled": "canceled", "run-open": "completed"}
