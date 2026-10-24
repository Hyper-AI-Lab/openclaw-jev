"""The status vocabulary (app/status.py), its named sets, and the stored spellings they keep (rule 16)."""
import json

import pytest
from temporalio.worker.workflow_sandbox import SandboxRestrictions
from temporalio.worker.workflow_sandbox._importer import Importer
from temporalio.worker.workflow_sandbox._restrictions import RestrictionContext

import app.status
from app.status import (
    ACTIVE,
    AGENT_STEP_OUTCOMES,
    HANDED_OFF,
    IDLE_BLOCKING,
    IN_PROGRESS,
    RUN_STATE_FOR_TASK_STATUS,
    RUN_TERMINAL,
    TERMINAL,
    WAITING,
    WORKFLOW_OWNED,
    RunState,
    StepStatus,
    TaskStatus,
)

ENUMS = (TaskStatus, RunState, StepStatus)


def test_the_vocabulary_is_pinned():
    assert [m.value for m in TaskStatus] == [
        "created", "running", "pending_user_input", "blocked", "deploying", "completed", "failed", "compensated",
        "stopped_by_user", "cancelled"]
    assert [m.value for m in RunState] == [
        "created", "running", "queued", "paused", "awaiting_input", "awaiting_approval", "waiting_external", "blocked",
        "deploying", "completed", "failed_terminal", "stopped_by_user", "compensated", "canceled", "superseded"]
    assert [m.value for m in StepStatus] == [
        "pending", "running", "completed", "failed", "blocked", "needs_replan", "stopped_by_user", "compensated"]
    for enum in ENUMS:
        assert all(m.value == m.name.lower() for m in enum)


@pytest.mark.parametrize("member", [m for enum in ENUMS for m in enum], ids=lambda m: f"{type(m).__name__}.{m.name}")
def test_members_behave_as_their_strings(member):
    assert member == member.value
    assert hash(member) == hash(member.value)
    assert json.dumps(member) == json.dumps(member.value)
    assert f"{member}" == member.value


def test_the_vocabulary_imports_inside_the_workflow_sandbox():
    """Imported fresh under the sandbox's default restrictions, not passed through from the host, the module raises no
    restriction error: it is pure."""
    with Importer(SandboxRestrictions.default, RestrictionContext()).applied():
        sandboxed = __import__("app.status", fromlist=["TaskStatus"])
    assert sandboxed is not app.status
    assert sandboxed.TaskStatus.CANCELLED == "cancelled" and sandboxed.RunState.CANCELED == "canceled"


def test_the_named_sets_are_pinned():
    assert TERMINAL == {"completed", "failed", "compensated", "stopped_by_user", "cancelled"}
    assert IN_PROGRESS == {"created", "running"}
    assert WAITING == {"pending_user_input", "blocked"}
    assert HANDED_OFF == {"deploying"}
    assert ACTIVE == {"created", "running", "pending_user_input", "blocked", "deploying"}
    assert WORKFLOW_OWNED == {"created", "running", "pending_user_input", "blocked"}
    assert IDLE_BLOCKING == {"created", "running", "pending_user_input"}
    assert RUN_TERMINAL == {"completed", "failed_terminal", "stopped_by_user", "compensated", "canceled", "superseded"}
    assert AGENT_STEP_OUTCOMES == {"completed", "pending", "failed", "stopped_by_user", "blocked", "needs_replan"}
    assert dict(RUN_STATE_FOR_TASK_STATUS) == {
        "completed": "completed", "failed": "failed_terminal", "cancelled": "canceled",
        "stopped_by_user": "stopped_by_user", "compensated": "compensated"}
    for named in (TERMINAL, IN_PROGRESS, WAITING, HANDED_OFF, ACTIVE, WORKFLOW_OWNED, IDLE_BLOCKING, RUN_TERMINAL,
                  AGENT_STEP_OUTCOMES):
        assert type(named) is frozenset
    with pytest.raises(TypeError):
        RUN_STATE_FOR_TASK_STATUS["failed"] = "failed"


def test_the_sets_partition_the_task_statuses():
    assert ACTIVE | TERMINAL == set(TaskStatus) and not ACTIVE & TERMINAL
    assert IN_PROGRESS | WAITING | HANDED_OFF == ACTIVE
    assert not IN_PROGRESS & WAITING and not IN_PROGRESS & HANDED_OFF and not WAITING & HANDED_OFF
    assert IDLE_BLOCKING < ACTIVE and TaskStatus.DEPLOYING not in IDLE_BLOCKING
    assert set(RUN_STATE_FOR_TASK_STATUS) == TERMINAL
    assert set(RUN_STATE_FOR_TASK_STATUS.values()) <= RUN_TERMINAL
