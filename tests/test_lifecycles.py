"""The per-type lifecycle declarations in app/status.py: every registered workflow type is declared once, each type's
code names only the task statuses and run states it declares, and the declarations fit the vocabulary's sets."""
import ast
import dataclasses
import inspect

import pytest
from temporalio import workflow

import worker
from app.status import LIFECYCLES, NO_TASK_STATUS, TERMINAL, WAITING, RunState, TaskStatus
from tests.test_status_vocabulary import status_member, workflow_status_values

# By Temporal workflow type name, the name LIFECYCLES and NO_TASK_STATUS use; a @workflow.defn(name=...) makes it
# differ from the class name.
REGISTERED = {workflow._Definition.must_from_class(cls).name: cls for cls in worker.WORKFLOWS}


def _workflow_classes(workflow_class):
    """The classes of the type's MRO defined in workflow code: its own and the mixins it uses."""
    return [cls for cls in workflow_class.__mro__ if cls.__module__.startswith("app.workflows.")]


def _named(workflow_class):
    """The TaskStatus and RunState members in G7's contexts across the type's workflow classes."""
    members = {(node.value.id, node.attr) for cls in _workflow_classes(workflow_class)
               for _clause, node in workflow_status_values(ast.parse(inspect.getsource(cls)), status_member)}
    return ({TaskStatus[name] for enum, name in members if enum == "TaskStatus"},
            {RunState[name] for enum, name in members if enum == "RunState"})


def test_the_lifecycles_are_pinned():
    generic, catalog, coding = (
        LIFECYCLES[name] for name in ("GenericTaskWorkflow", "CatalogTaskWorkflow", "CodingTaskWorkflow"))
    assert set(LIFECYCLES) == {"GenericTaskWorkflow", "CatalogTaskWorkflow", "CodingTaskWorkflow"}
    assert generic.task_statuses == {
        "running", "pending_user_input", "completed", "failed", "compensated", "stopped_by_user"}
    assert generic.run_states == {"running", "completed", "failed_terminal", "compensated", "stopped_by_user"}
    assert generic.waits == {"pending_user_input"}
    assert generic.outcomes == {"completed", "failed", "compensated", "stopped_by_user"}
    assert generic.compensation == "compensated"
    assert catalog.task_statuses == generic.task_statuses | {"cancelled"}
    assert catalog.run_states == {
        "running", "awaiting_approval", "waiting_external", "blocked", "completed", "failed_terminal", "compensated",
        "stopped_by_user", "canceled"}
    assert catalog.waits == {"pending_user_input"}
    assert catalog.outcomes == generic.outcomes | {"cancelled"}
    assert catalog.compensation == "compensated"
    assert coding.task_statuses == {
        "running", "blocked", "pending_user_input", "deploying", "completed", "failed", "stopped_by_user", "cancelled"}
    assert coding.run_states == {
        "running", "queued", "paused", "awaiting_input", "awaiting_approval", "deploying", "completed",
        "failed_terminal", "stopped_by_user", "canceled"}
    assert coding.waits == {"blocked", "pending_user_input"}
    assert coding.outcomes == {"completed", "failed", "stopped_by_user", "cancelled"}
    assert coding.compensation is None
    assert NO_TASK_STATUS == {
        "CatalogStepChildWorkflow", "GenericExecuteChildWorkflow", "IntakeWorkflow", "DeepRecallWorkflow"}
    with pytest.raises(dataclasses.FrozenInstanceError):
        generic.waits = frozenset()
    with pytest.raises(TypeError):
        LIFECYCLES["GenericTaskWorkflow"] = coding


def test_every_registered_workflow_type_is_declared_exactly_once():
    assert len(REGISTERED) == len(worker.WORKFLOWS)
    assert not set(LIFECYCLES) & NO_TASK_STATUS
    assert set(LIFECYCLES) | NO_TASK_STATUS == set(REGISTERED)


def test_the_mixins_count_for_the_types_that_use_them():
    assert {cls.__name__ for cls in _workflow_classes(REGISTERED["CatalogTaskWorkflow"])} == {
        "CatalogTaskWorkflow", "AttachedMessages", "EvaluatorRetry", "DeepRecallPhase"}


@pytest.mark.parametrize("name", sorted(REGISTERED))
def test_each_type_names_only_what_it_declares(name):
    task_statuses, run_states = _named(REGISTERED[name])
    if name in NO_TASK_STATUS:
        assert not task_statuses and not run_states
        return
    lifecycle = LIFECYCLES[name]
    # The walk found the type's writes: a walk that reads nothing would pass on anything.
    assert task_statuses and run_states
    assert task_statuses <= lifecycle.task_statuses, sorted(task_statuses - lifecycle.task_statuses)
    assert run_states <= lifecycle.run_states, sorted(run_states - lifecycle.run_states)


@pytest.mark.parametrize("name", sorted(LIFECYCLES))
def test_the_declarations_fit_the_vocabulary(name):
    lifecycle = LIFECYCLES[name]
    # Members of the right enum: a RunState member equals the TaskStatus member of the same value.
    assert all(type(status) is TaskStatus
               for status in (*lifecycle.task_statuses, *lifecycle.waits, *lifecycle.outcomes))
    assert all(type(state) is RunState for state in lifecycle.run_states)
    assert lifecycle.compensation is None or type(lifecycle.compensation) is TaskStatus
    assert lifecycle.outcomes <= TERMINAL
    assert lifecycle.waits <= WAITING
    assert lifecycle.waits | lifecycle.outcomes <= lifecycle.task_statuses
    assert lifecycle.compensation is None or lifecycle.compensation in lifecycle.outcomes
