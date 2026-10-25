"""The task state machine (app/status.py's TASK_TRANSITIONS, task_move_allowed, run_move_allowed): every recorded
workflow history fits it, and nothing leaves a terminal status."""
import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter

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

