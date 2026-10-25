"""The per-type lifecycle declarations in app/status.py: every registered workflow type is declared once, each type's
code names only the task statuses and run states it declares, and the declarations fit the vocabulary's sets."""
import ast
import dataclasses
import inspect

import pytest
from temporalio import workflow

import worker
from app.status import ACTIVE, LIFECYCLES, NO_TASK_STATUS, TASK_TRANSITIONS, TERMINAL, WAITING, RunState, TaskStatus
from tests.test_status_vocabulary import helper_parameters, status_member, status_slots, workflow_status_values

# By Temporal workflow type name, the name LIFECYCLES and NO_TASK_STATUS use; a @workflow.defn(name=...) makes it
# differ from the class name.
REGISTERED = {workflow._Definition.must_from_class(cls).name: cls for cls in worker.WORKFLOWS}


def _workflow_classes(workflow_class):
    """The classes of the type's MRO defined in workflow code: its own and the mixins it uses."""
    return [cls for cls in workflow_class.__mro__ if cls.__module__.startswith("app.workflows.")]


def _trees(workflow_class):
    return {cls.__name__: ast.parse(inspect.getsource(cls)) for cls in _workflow_classes(workflow_class)}


def _named(workflow_class):
    """The TaskStatus and RunState members in G7's contexts across the type's workflow classes."""
    members = {(node.value.id, node.attr) for tree in _trees(workflow_class).values()
               for _clause, node in workflow_status_values(tree, status_member)}
    return ({TaskStatus[name] for enum, name in members if enum == "TaskStatus"},
            {RunState[name] for enum, name in members if enum == "RunState"})


def _slots(trees):
    """(class, enum, member) for each member in a status slot of the classes' code, its helpers read across them all."""
    helpers = {name: params for tree in trees.values() for name, params in helper_parameters(tree).items()}
    return [(cls, enum, node) for cls, tree in trees.items() for enum, node in status_slots(tree, helpers)]


def _misplaced(trees):
    """Each member written into a status slot of another enum, or of no known enum. A member of the wrong enum has a
    right value (StepStatus.RUNNING equals TaskStatus.RUNNING), so only the slot shows that a task status or a run
    state went through the wrong vocabulary."""
    return [f"{cls}:{node.lineno} {node.value.id}.{node.attr} in a {enum.__name__ if enum else 'unknown'} slot"
            for cls, enum, node in _slots(trees) if enum is None or node.value.id != enum.__name__]


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
    assert not _misplaced(_trees(REGISTERED[name]))
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


@pytest.mark.parametrize("name", sorted(LIFECYCLES))
def test_the_lifecycles_fit_the_table(name):
    """WP04-10 (c)'s last part: every status a type declares is one the table lets an active task move to."""
    lifecycle = LIFECYCLES[name]
    for status in lifecycle.task_statuses | lifecycle.waits | lifecycle.outcomes:
        assert TASK_TRANSITIONS[status] & ACTIVE, status


def _snippet(source):
    return {"Snippet": ast.parse(source)}


@pytest.mark.parametrize("source", [
    'workflow.execute_activity(update_task_status, {"task_id": task_id, "status": StepStatus.RUNNING})',
    'workflow.execute_activity(update_process_state, {"process_run_id": run_id, "state": TaskStatus.COMPLETED})',
    'workflow.execute_activity(finalize_task_failure, {"task_id": task_id, "task_status": RunState.FAILED_TERMINAL})',
    'workflow.execute_activity(finalize_task_failure, {"task_id": t, "process_state": StepStatus.FAILED if x else 1})',
    'return {"status": StepStatus.COMPLETED, "task_id": task_id}',
    'return {"status": TaskStatus.PENDING, "step_id": step_id}',
    "class W:\n    async def _status(self, task_status, process_state):\n        pass\n\n"
    "    async def run(self):\n        await self._status(StepStatus.RUNNING, RunState.RUNNING)",
    "class W:\n    async def _close(self, task_status, process_state):\n        pass\n\n"
    "    async def run(self):\n        await self._close(TaskStatus.FAILED, process_state=TaskStatus.CANCELLED)",
    "class W:\n    async def _stop_task(self, task_id, status=StepStatus.STOPPED_BY_USER):\n        pass",
    # A member given to a helper's parameter that holds no status.
    "class W:\n    async def _ask(self, message, process_state):\n        pass\n\n"
    "    async def run(self):\n        await self._ask(TaskStatus.BLOCKED, RunState.AWAITING_INPUT)",
])
def test_a_member_of_the_wrong_enum_in_a_status_slot_is_found(source):
    assert _misplaced(_snippet(source))


@pytest.mark.parametrize("source", [
    'workflow.execute_activity(update_task_status, {"task_id": task_id, "status": TaskStatus.RUNNING})',
    'workflow.execute_activity(finalize_task_failure, {"task_id": t, "task_status": TaskStatus.FAILED,\n'
    '    "process_state": RunState.FAILED_TERMINAL if hard else RunState.COMPENSATED})',
    'workflow.execute_activity(record_step, {"process_run_id": r, "status": StepStatus.COMPLETED})',
    'return {"status": StepStatus.PENDING, "reason": why, "step_id": step_id}',
    "class W:\n    async def _status(self, task_status, process_state, *, minutes=None):\n        pass\n\n"
    "    async def run(self):\n        await self._status(TaskStatus.BLOCKED, RunState.PAUSED, minutes=5)",
    "class W:\n    async def _stop_task(self, task_id, status=TaskStatus.STOPPED_BY_USER):\n        pass\n\n"
    "    async def run(self):\n        await self._stop_task(task_id, status=TaskStatus.CANCELLED)",
    # Reads and locals are no slot: a step outcome compared or kept under a name ending in status.
    'if status in (StepStatus.FAILED, StepStatus.BLOCKED):\n    step_status = StepStatus.COMPLETED',
])
def test_a_member_of_its_own_enum_in_a_status_slot_passes(source):
    assert not _misplaced(_snippet(source))


def test_the_slot_walk_reads_the_workflows_writes():
    """A walk that finds no slot would pass on anything: each declared type writes both a task status and a run state
    through its slots."""
    for name in LIFECYCLES:
        enums = {enum for _cls, enum, _node in _slots(_trees(REGISTERED[name]))}
        assert {TaskStatus, RunState} <= enums, name
