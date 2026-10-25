"""The task state machine (app/status.py's TASK_TRANSITIONS, task_move_allowed, run_move_allowed): every recorded
workflow history fits it, and nothing leaves a terminal status."""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter

from app import metrics
from app.db.models import Base, ProcessRun, Task
from app.status import (
    ACTIVE,
    RUN_TERMINAL,
    TASK_TRANSITIONS,
    TERMINAL,
    RunState,
    StepStatus,
    TaskStatus,
    run_move_allowed,
    task_move_allowed,
)
from app.transitions import RETRIES, Transition, set_run_state, set_task_status

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_the_table_is_pinned():
    """D-5, literally: a change of the state machine is a reviewed edit of this test."""
    assert {to: set(allowed) for to, allowed in TASK_TRANSITIONS.items()} == {
        "created": {"created", "pending_user_input"},
        "running": {"created", "running", "pending_user_input", "blocked"},
        "pending_user_input": {"created", "running", "pending_user_input", "blocked"},
        "blocked": {"created", "running", "blocked"},
        "deploying": {"running", "deploying"},
        **{terminal: {"created", "running", "pending_user_input", "blocked", "deploying"} for terminal in TERMINAL},
    }
    assert all(type(to) is TaskStatus and type(allowed) is frozenset for to, allowed in TASK_TRANSITIONS.items())
    with pytest.raises(TypeError):
        TASK_TRANSITIONS["running"] = frozenset()


def test_terminal_is_final():
    for terminal in TERMINAL:
        for to in TaskStatus:
            if to != terminal:
                assert not task_move_allowed(terminal, to), (terminal, to)
    for ended in RUN_TERMINAL:
        for to in RunState:
            assert run_move_allowed(ended, to) == (to == ended), (ended, to)


def test_an_active_task_may_refresh_and_finish():
    for status in ACTIVE:
        assert task_move_allowed(status, status)
        assert all(task_move_allowed(status, terminal) for terminal in TERMINAL)
    assert not task_move_allowed(TaskStatus.DEPLOYING, TaskStatus.RUNNING)
    assert not task_move_allowed(TaskStatus.BLOCKED, TaskStatus.DEPLOYING)


def test_a_run_that_has_not_ended_may_move_to_any_run_state():
    for prev in set(RunState) - RUN_TERMINAL:
        assert all(run_move_allowed(prev, to) for to in RunState)
    # A run stored with a task spelling has not ended in the run vocabulary.
    assert run_move_allowed("cancelled", RunState.CANCELED)


@pytest.mark.parametrize("to", ["pending", "needs_replan", "waiting_agent", "failed_terminal", "", None])
def test_an_unknown_task_status_is_refused(to):
    assert not task_move_allowed(TaskStatus.RUNNING, to)


@pytest.mark.parametrize("to", ["cancelled", "failed", "pending", "", None])
def test_an_unknown_run_state_is_refused(to):
    assert not run_move_allowed(RunState.RUNNING, to)


def test_a_member_of_another_enum_is_refused():
    """A StepStatus or RunState member equals the TaskStatus member of the same value, so only its type tells."""
    assert StepStatus.RUNNING == TaskStatus.RUNNING
    assert not task_move_allowed(TaskStatus.CREATED, StepStatus.RUNNING)
    assert not task_move_allowed(TaskStatus.RUNNING, RunState.COMPLETED)
    assert not run_move_allowed(RunState.RUNNING, TaskStatus.COMPLETED)
    assert not run_move_allowed(RunState.RUNNING, StepStatus.COMPLETED)
    assert task_move_allowed(TaskStatus.CREATED, "running") and task_move_allowed("created", TaskStatus.RUNNING)
    assert run_move_allowed(RunState.RUNNING, "completed") and run_move_allowed("running", RunState.COMPLETED)


# The activities whose scheduled input moves the task, and the status each one writes.
TASK_WRITES = {
    "update_task_status": lambda payload: payload.get("status"),
    "finalize_task_failure": lambda payload: payload.get("task_status", "failed") if payload.get("task_id") else None,
    "execute_compensation": lambda payload: "compensated" if payload.get("task_id") else None,
}


def recorded_task_writes():
    """(history, input of the run, the task statuses it wrote in order) for every recorded workflow history."""
    converter = DataConverter.default.payload_converter
    for path in sorted(FIXTURES.glob("*_histories*.json")):
        for key, item in json.loads(path.read_text()).items():
            events = WorkflowHistory.from_json(item["workflow_id"], item["history"]).events
            start = events[0].workflow_execution_started_event_attributes
            run_input = converter.from_payloads(start.input.payloads)[0] if start.input.payloads else {}
            writes = []
            for event in events:
                if not event.HasField("activity_task_scheduled_event_attributes"):
                    continue
                scheduled = event.activity_task_scheduled_event_attributes
                write = TASK_WRITES.get(scheduled.activity_type.name)
                status = write(converter.from_payloads(scheduled.input.payloads)[0]) if write else None
                if status is not None:
                    writes.append(status)
            yield f"{path.name}:{key}", run_input, writes


def test_every_recorded_transition_is_allowed():
    histories = list(recorded_task_writes())
    # The walk read the histories' writes: a walk that reads none would pass on anything.
    assert sum(len(writes) for _name, _input, writes in histories) >= 20
    for name, run_input, writes in histories:
        if not writes:
            continue
        # The coding reply run, which the deploy unit starts after a self-deploy, finds its task deploying.
        first = TaskStatus.DEPLOYING if isinstance(run_input, dict) and run_input.get("report") else TaskStatus.CREATED
        for prev, to in zip([first, *writes], writes):
            assert task_move_allowed(prev, to), f"{name}: {prev} -> {to}"


# The guarded writer, app/transitions.py, on a SQLite session.
LOGGER = "rmp.transitions"


@pytest.fixture
async def factory(tmp_path, monkeypatch):
    monkeypatch.setenv("RMP_METRICS_PATH", str(tmp_path / "metrics.json"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'transitions.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Task.__table__, ProcessRun.__table__])
    yield sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def seed(factory, *rows):
    async with factory() as db:
        db.add_all(rows)
        await db.commit()


async def stored(factory, model, row_id):
    async with factory() as db:
        return await db.get(model, row_id)


def refusals(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def later(minutes):
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes)


async def test_completed_to_running_is_refused_and_logged(factory, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await seed(factory, Task(id="t1", goal="g", status="completed"),
               ProcessRun(id="r1", task_id="t1", current_state="completed"))

    async with factory() as db:
        task = await set_task_status(db, "t1", TaskStatus.RUNNING, actor="test", cause="late write")
        run = await set_run_state(db, "r1", RunState.RUNNING, actor="test", cause="late write")
        await db.commit()

    assert task == Transition("refused", "completed") and not task.changed
    assert run == Transition("refused", "completed")
    assert (await stored(factory, Task, "t1")).status == "completed"
    assert (await stored(factory, ProcessRun, "r1")).current_state == "completed"
    assert refusals(caplog) == ["task transition refused: t1 completed -> running actor=test cause=late write",
                                "run transition refused: r1 completed -> running actor=test cause=late write"]
    assert refusals(caplog)[0].startswith("task transition refused: ")
    counters = metrics.get_counters()
    assert counters["task_transition_refused"] == 1 and counters["run_transition_refused"] == 1


async def test_an_active_task_moves_and_a_finished_one_stays(factory, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await seed(factory, Task(id="t1", goal="g", status="running", next_check_at=later(30)),
               Task(id="t2", goal="g", status="cancelled", supplementary_context={"closed_reason": "first"}))

    async with factory() as db:
        moved = await set_task_status(db, "t1", "failed", actor="test", cause="it broke", closed_reason="broke")
        again = await set_task_status(db, "t2", TaskStatus.CANCELLED, actor="test", cause="again", closed_reason="2nd")
        missing = await set_task_status(db, "t-none", TaskStatus.FAILED, actor="test", cause="gone")
        await db.commit()

    assert moved == Transition("changed", "running") and moved.changed
    assert again == Transition("unchanged", "cancelled") and missing == Transition("missing")
    failed, cancelled = await stored(factory, Task, "t1"), await stored(factory, Task, "t2")
    assert failed.status == "failed" and failed.next_check_at is None
    assert failed.supplementary_context == {"closed_reason": "broke"}
    # A repeat of a terminal status changes nothing, and is no refusal.
    assert cancelled.supplementary_context == {"closed_reason": "first"}
    assert refusals(caplog) == []


async def test_a_refresh_updates_the_next_check_only(factory, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await seed(factory, Task(id="t1", goal="g", status="pending_user_input", next_check_at=later(5),
                             supplementary_context={"source": "slack"}))
    before = await stored(factory, Task, "t1")
    check = later(60).replace(microsecond=0)

    async with factory() as db:
        kept = await set_task_status(db, "t1", TaskStatus.PENDING_USER_INPUT, actor="test", cause="refresh")
        refreshed = await set_task_status(db, "t1", TaskStatus.PENDING_USER_INPUT, actor="test", cause="refresh",
                                          closed_reason="not merged", next_check_at=check)
        await db.commit()

    assert kept == refreshed == Transition("unchanged", "pending_user_input")
    task = await stored(factory, Task, "t1")
    assert task.status == "pending_user_input" and task.next_check_at == check
    assert task.supplementary_context == {"source": "slack"}
    assert before.next_check_at != check and refusals(caplog) == []


@pytest.mark.parametrize("to", ["pending", "needs_replan", "", StepStatus.COMPLETED, RunState.COMPLETED])
async def test_unknown_status_is_refused(factory, caplog, to):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await seed(factory, Task(id="t1", goal="g", status="running"), Task(id="t2", goal="g", status="completed"))

    async with factory() as db:
        active = await set_task_status(db, "t1", to, actor="test", cause="bad value")
        # A member of another enum with the row's own value is refused too, not taken for an unchanged status.
        finished = await set_task_status(db, "t2", to, actor="test", cause="bad value")
        await db.commit()

    assert active == Transition("refused", "running") and finished == Transition("refused", "completed")
    assert (await stored(factory, Task, "t1")).status == "running"
    assert refusals(caplog) == [f"task transition refused: t1 running -> {to} actor=test cause=bad value",
                                f"task transition refused: t2 completed -> {to} actor=test cause=bad value"]


@pytest.mark.parametrize("to", ["cancelled", "pending", TaskStatus.COMPLETED])
async def test_an_unknown_run_state_is_refused(factory, caplog, to):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await seed(factory, ProcessRun(id="r1", task_id="t1", current_state="running"))

    async with factory() as db:
        assert await set_run_state(db, "r1", to, actor="test", cause="bad value") == Transition("refused", "running")
        await db.commit()

    assert (await stored(factory, ProcessRun, "r1")).current_state == "running"
    assert refusals(caplog) == [f"run transition refused: r1 running -> {to} actor=test cause=bad value"]


async def test_an_in_session_task_sees_the_new_status(factory):
    await seed(factory, Task(id="t1", goal="g", status="running", next_check_at=later(30),
                             supplementary_context={"source": "slack"}))

    async with factory() as db:
        task = await db.get(Task, "t1")
        moved = await set_task_status(db, "t1", TaskStatus.CANCELLED, actor="test", cause="cancel",
                                      closed_reason="api_cancel")
        # Read before any commit or refresh: the update synchronised the loaded row.
        assert moved.changed and task.status == "cancelled" and task.next_check_at is None
        assert task.supplementary_context == {"source": "slack", "closed_reason": "api_cancel"}
        await db.commit()

    assert (await stored(factory, Task, "t1")).status == "cancelled"


async def test_a_run_ends_with_its_lease_released(factory):
    await seed(factory, ProcessRun(id="r1", task_id="t1", current_state="running", lease_owner="w-1",
                                   next_check_at=later(5)),
               ProcessRun(id="r2", task_id="t2", current_state="awaiting_approval", lease_owner="w-2"))
    check = later(60).replace(microsecond=0)

    async with factory() as db:
        ended = await set_run_state(db, "r1", RunState.CANCELED, actor="test", cause="cancel", ended=True)
        waiting = await set_run_state(db, "r2", RunState.AWAITING_INPUT, actor="test", cause="ask",
                                      release_lease=True, next_check_at=check)
        await db.commit()

    assert ended == Transition("changed", "running") and waiting == Transition("changed", "awaiting_approval")
    r1, r2 = await stored(factory, ProcessRun, "r1"), await stored(factory, ProcessRun, "r2")
    assert r1.current_state == "canceled" and r1.ended_at is not None and r1.lease_owner is None
    assert r1.next_check_at is None
    assert r2.current_state == "awaiting_input" and r2.ended_at is None and r2.lease_owner is None
    assert r2.next_check_at == check


def racing(db, moves, *, other=None):
    """Before each of ``db``'s UPDATEs, the next of ``moves`` is written to the task row, so the row no longer holds
    the value the helper read: committed by a session of ``other``, or written on ``db``'s own connection once ``db``
    holds SQLite's one write lock. Returns the list of UPDATEs ``db`` ran."""
    original, updates = db.execute, []

    async def execute(statement, *args, **kwargs):
        if getattr(statement, "is_dml", False):
            updates.append(statement)
            if moves:
                move = sa_update(Task).where(Task.id == "t1").values(status=moves.pop(0))
                if other is None:
                    await original(move)
                else:
                    async with other() as writer:
                        await writer.execute(move)
                        await writer.commit()
        return await original(statement, *args, **kwargs)

    db.execute = execute
    return updates


async def test_a_lost_race_is_retried(factory, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await seed(factory, Task(id="t1", goal="g", status="running"))

    async with factory() as db:
        updates = racing(db, ["pending_user_input"], other=factory)
        moved = await set_task_status(db, "t1", TaskStatus.COMPLETED, actor="test", cause="done")
        await db.commit()

    # The first UPDATE found the row moved on; the helper read it again and applied the move from the new value.
    assert moved == Transition("changed", "pending_user_input") and len(updates) == 2
    assert (await stored(factory, Task, "t1")).status == "completed" and refusals(caplog) == []


async def test_a_race_lost_every_time_is_refused_as_contended(factory, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await seed(factory, Task(id="t1", goal="g", status="running"))

    async with factory() as db:
        updates = racing(db, ["blocked", "running", "blocked", "running"])
        moved = await set_task_status(db, "t1", TaskStatus.COMPLETED, actor="test", cause="done")
        await db.commit()

    # One try and RETRIES more, each lost; then the refusal, with its own cause and the value read last.
    assert len(updates) == 1 + RETRIES == 4
    assert moved == Transition("refused", "blocked")
    assert (await stored(factory, Task, "t1")).status == "running"
    assert refusals(caplog) == ["task transition refused: t1 blocked -> completed actor=test cause=contended"]
