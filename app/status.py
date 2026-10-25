"""RMP's one status vocabulary: task statuses, process-run states and step statuses, and the named sets over them.

Each value is the string RMP stores today. Rule 16: no stored status is renamed, so no value here ever changes. That
is why a task is ``cancelled`` and a run is ``canceled``: both spellings are stored, and RUN_STATE_FOR_TASK_STATUS
translates a task status to its run state.

Members are ``str``s (``StrEnum``): each compares, hashes and JSON-encodes exactly like its value, so SQL binds, set
lookups on loaded rows and the payloads workflows send are unchanged. The module imports nothing from ``app`` and does
no I/O, so workflow code can import it.

TASK_TRANSITIONS is the task state machine, with task_move_allowed and run_move_allowed; app/transitions.py writes
every task status and run state through them.

LIFECYCLES is R-05's per-type declaration: for each task workflow type, the task statuses and run states its code
writes, its waits, its outcomes and its compensation. R-05's other parts live elsewhere: success and failure criteria,
the evidence schema and template versioning in WP-28, retryable failure classes in WP-05, idempotency rules in WP-23,
and escalation in WP-39 and WP-33.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum
from types import MappingProxyType


class TaskStatus(StrEnum):
    """``tasks.status``."""

    CREATED = "created"
    RUNNING = "running"
    PENDING_USER_INPUT = "pending_user_input"
    BLOCKED = "blocked"
    DEPLOYING = "deploying"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATED = "compensated"
    STOPPED_BY_USER = "stopped_by_user"
    CANCELLED = "cancelled"


class RunState(StrEnum):
    """``process_runs.current_state``."""

    CREATED = "created"
    RUNNING = "running"
    QUEUED = "queued"
    PAUSED = "paused"
    AWAITING_INPUT = "awaiting_input"
    AWAITING_APPROVAL = "awaiting_approval"
    WAITING_EXTERNAL = "waiting_external"
    BLOCKED = "blocked"
    DEPLOYING = "deploying"
    COMPLETED = "completed"
    FAILED_TERMINAL = "failed_terminal"
    STOPPED_BY_USER = "stopped_by_user"
    COMPENSATED = "compensated"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"


class StepStatus(StrEnum):
    """``steps.status``."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NEEDS_REPLAN = "needs_replan"
    STOPPED_BY_USER = "stopped_by_user"
    COMPENSATED = "compensated"


# A finished task: nothing runs for it again. Read by the indexer, /retry, GET /tasks/{id}, the reconciler and checks.
TERMINAL = frozenset({
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.COMPENSATED,
    TaskStatus.STOPPED_BY_USER,
    TaskStatus.CANCELLED,
})
# A workflow is running the task, or is about to start it. Read by the reconciler's repairs and the sentinel.
IN_PROGRESS = frozenset({TaskStatus.CREATED, TaskStatus.RUNNING})
# The workflow waits for Kirill or for a coding slot. Read through ACTIVE and WORKFLOW_OWNED.
WAITING = frozenset({TaskStatus.PENDING_USER_INPUT, TaskStatus.BLOCKED})
# The coding workflow has handed the task to the self-deploy. Read through ACTIVE.
HANDED_OFF = frozenset({TaskStatus.DEPLOYING})
# Every status that is not terminal. Read by POST /tasks's recovery, the coding slot holder and the stuck canaries.
ACTIVE = IN_PROGRESS | WAITING | HANDED_OFF
# A workflow runs the task or waits in it. Read by active_user_task, suspend-all, intake, spawn and the dashboard.
WORKFLOW_OWNED = IN_PROGRESS | WAITING
# The deploy's idle gate, the canary scripts' skip and the plugin's cron skip: work a restart would hurt. `deploying`
# is out because the self-deploy waits for idle while its own task is deploying; `blocked` is out because a queued or
# usage-paused coding job holds no run a restart would hurt. WP-14 removes long human waits from this set.
IDLE_BLOCKING = IN_PROGRESS | {TaskStatus.PENDING_USER_INPUT}

# A finished run. Read by ensure_process_run and the reconciler.
RUN_TERMINAL = frozenset({
    RunState.COMPLETED,
    RunState.FAILED_TERMINAL,
    RunState.STOPPED_BY_USER,
    RunState.COMPENSATED,
    RunState.CANCELED,
    RunState.SUPERSEDED,
})

# What Aura's step report may say about a step. Read by the decision engine and the step parser.
AGENT_STEP_OUTCOMES = frozenset({
    StepStatus.COMPLETED,
    StepStatus.PENDING,
    StepStatus.FAILED,
    StepStatus.STOPPED_BY_USER,
    StepStatus.BLOCKED,
    StepStatus.NEEDS_REPLAN,
})

# The run state of a task that ended with this status. Read by update_process_state and the reconciler's run close.
RUN_STATE_FOR_TASK_STATUS = MappingProxyType({
    TaskStatus.COMPLETED: RunState.COMPLETED,
    TaskStatus.FAILED: RunState.FAILED_TERMINAL,
    TaskStatus.CANCELLED: RunState.CANCELED,
    TaskStatus.STOPPED_BY_USER: RunState.STOPPED_BY_USER,
    TaskStatus.COMPENSATED: RunState.COMPENSATED,
})

# The task state machine, as "to <- allowed from": every move RMP's code makes, and no way out of a terminal status.
# Read through task_move_allowed by app/transitions.py, the one writer of task statuses.
TASK_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = MappingProxyType({
    TaskStatus.CREATED: frozenset({TaskStatus.CREATED, TaskStatus.PENDING_USER_INPUT}),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.CREATED, TaskStatus.RUNNING, TaskStatus.PENDING_USER_INPUT, TaskStatus.BLOCKED}),
    TaskStatus.PENDING_USER_INPUT: frozenset({
        TaskStatus.CREATED, TaskStatus.RUNNING, TaskStatus.PENDING_USER_INPUT, TaskStatus.BLOCKED}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.CREATED, TaskStatus.RUNNING, TaskStatus.BLOCKED}),
    TaskStatus.DEPLOYING: frozenset({TaskStatus.RUNNING, TaskStatus.DEPLOYING}),
    **{status: ACTIVE for status in TERMINAL},
})


def _names_a_member(enum: type[StrEnum], value: object) -> bool:
    """``value`` is one of ``enum``'s values, as its string or as the member itself. A member of another enum is not,
    even with the same value: a task status written as ``StepStatus.RUNNING`` is a mistake its value would hide."""
    return isinstance(value, str) and value in enum and (not isinstance(value, Enum) or isinstance(value, enum))


def task_move_allowed(prev: str | None, to: str) -> bool:
    """Whether a task may go from ``prev`` to ``to``: a move TASK_TRANSITIONS allows, which includes the refresh of
    every status that is not terminal. An unknown ``to`` is refused."""
    return _names_a_member(TaskStatus, to) and prev in TASK_TRANSITIONS[to]


def run_move_allowed(prev: str | None, to: str) -> bool:
    """Whether a run may go from ``prev`` to ``to``: a terminal run state is final, except that it may stay as it is;
    a run that has not ended may move to any run state. An unknown ``to`` is refused."""
    if not _names_a_member(RunState, to):
        return False
    return prev not in RUN_TERMINAL or prev == to


@dataclass(frozen=True)
class Lifecycle:
    """What one task workflow type does with its task's status.

    ``task_statuses`` and ``run_states`` are everything its code writes; ``waits`` the statuses in which it waits for
    Kirill or for a coding slot; ``outcomes`` the terminal statuses it ends in; ``compensation`` the status a compensated
    task ends in, or None for a type that does not compensate.
    """

    task_statuses: frozenset[TaskStatus]
    run_states: frozenset[RunState]
    waits: frozenset[TaskStatus]
    outcomes: frozenset[TaskStatus]
    compensation: TaskStatus | None


_GENERIC = Lifecycle(
    task_statuses=frozenset({
        TaskStatus.RUNNING,
        TaskStatus.PENDING_USER_INPUT,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.COMPENSATED,
        TaskStatus.STOPPED_BY_USER,
    }),
    run_states=frozenset({
        RunState.RUNNING,
        RunState.COMPLETED,
        RunState.FAILED_TERMINAL,
        RunState.COMPENSATED,
        RunState.STOPPED_BY_USER,
    }),
    waits=frozenset({TaskStatus.PENDING_USER_INPUT}),
    outcomes=frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.COMPENSATED, TaskStatus.STOPPED_BY_USER}),
    compensation=TaskStatus.COMPENSATED,
)

# Each task workflow type's lifecycle, by its workflow type name. tests/test_lifecycles.py pins that each type's code
# names only what it declares. WP-14 reads the waits to take long human waits out of IDLE_BLOCKING.
LIFECYCLES = MappingProxyType({
    "GenericTaskWorkflow": _GENERIC,
    "CatalogTaskWorkflow": Lifecycle(
        task_statuses=_GENERIC.task_statuses | {TaskStatus.CANCELLED},
        run_states=frozenset({
            RunState.RUNNING,
            RunState.AWAITING_APPROVAL,
            RunState.WAITING_EXTERNAL,
            RunState.BLOCKED,
            RunState.COMPLETED,
            RunState.FAILED_TERMINAL,
            RunState.COMPENSATED,
            RunState.STOPPED_BY_USER,
            RunState.CANCELED,
        }),
        waits=frozenset({TaskStatus.PENDING_USER_INPUT}),
        outcomes=_GENERIC.outcomes | {TaskStatus.CANCELLED},
        compensation=TaskStatus.COMPENSATED,
    ),
    "CodingTaskWorkflow": Lifecycle(
        task_statuses=frozenset({
            TaskStatus.RUNNING,
            TaskStatus.BLOCKED,
            TaskStatus.PENDING_USER_INPUT,
            TaskStatus.DEPLOYING,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.STOPPED_BY_USER,
            TaskStatus.CANCELLED,
        }),
        run_states=frozenset({
            RunState.RUNNING,
            RunState.QUEUED,
            RunState.PAUSED,
            RunState.AWAITING_INPUT,
            RunState.AWAITING_APPROVAL,
            RunState.DEPLOYING,
            RunState.COMPLETED,
            RunState.FAILED_TERMINAL,
            RunState.STOPPED_BY_USER,
            RunState.CANCELED,
        }),
        waits=frozenset({TaskStatus.BLOCKED, TaskStatus.PENDING_USER_INPUT}),
        outcomes=frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED_BY_USER, TaskStatus.CANCELLED}),
        compensation=None,
    ),
})
# The registered workflow types that write no task status: the two step children, intake and deep recall.
NO_TASK_STATUS = frozenset({
    "CatalogStepChildWorkflow",
    "GenericExecuteChildWorkflow",
    "IntakeWorkflow",
    "DeepRecallWorkflow",
})
