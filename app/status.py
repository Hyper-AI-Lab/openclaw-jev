"""RMP's one status vocabulary: task statuses, process-run states and step statuses, and the named sets over them.

Each value is the string RMP stores today. Rule 16: no stored status is renamed, so no value here ever changes. That
is why a task is ``cancelled`` and a run is ``canceled``: both spellings are stored, and RUN_STATE_FOR_TASK_STATUS
translates a task status to its run state.

Members are ``str``s (``StrEnum``): each compares, hashes and JSON-encodes exactly like its value, so SQL binds, set
lookups on loaded rows and the payloads workflows send are unchanged. The module imports nothing from ``app`` and does
no I/O, so workflow code can import it.
"""
from enum import StrEnum
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
