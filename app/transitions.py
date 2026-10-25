"""The one writer of task statuses and run states (specs/WP-04.md D-6).

``set_task_status`` and ``set_run_state`` move one row in the caller's session and transaction, compare-and-set: each
reads the current value, refuses a move app/status.py forbids, and otherwise updates the row only where it still holds
the value it read. On Postgres the conditional UPDATE is atomic under READ COMMITTED, so a lost race matches no row;
the call then reads again and retries, at most RETRIES times. Writing the current value again is a refresh: the next
check is updated and nothing else. On a terminal row it changes nothing.

A refusal is logged at WARNING on ``rmp.transitions`` in one fixed line, ``task transition refused: <id> <from> ->
<to> actor=<actor> cause=<cause>`` (``run transition refused: …`` for a run), which the programme counts in the
journal, and counted as ``task_transition_refused`` (``run_transition_refused``). The caller commits, and runs its
side effects (registry index, metrics, alerts, events, notices) only when the outcome is ``changed``.

``actor`` is the writer: ``workflow:<type>``, ``api:<route>``, ``intake``, ``reconciler``, ``spawn``, ``scanner_sync``
or ``ops:<script>``. ``cause`` says why, in a few words.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProcessRun, Task
from app.metrics import inc as metrics_inc
from app.status import RUN_TERMINAL, TERMINAL, RunState, TaskStatus, run_move_allowed, task_move_allowed

logger = logging.getLogger("rmp.transitions")

# How often a compare-and-set that lost its row to another writer reads it again and retries.
RETRIES = 3
CHANGED, UNCHANGED, REFUSED, MISSING = "changed", "unchanged", "refused", "missing"
# The refusal lines, fixed: the programme counts them in the journal.
TASK_REFUSED = "task transition refused: %s %s -> %s actor=%s cause=%s"
RUN_REFUSED = "run transition refused: %s %s -> %s actor=%s cause=%s"


class _Keep:
    def __repr__(self) -> str:
        return "KEEP"


# The default of ``next_check_at``: the stored next check stays as it is.
KEEP: Any = _Keep()


@dataclass(frozen=True)
class Transition:
    """What one call did. ``outcome`` is changed, unchanged, refused or missing; ``previous`` the value it read last
    (None for a missing row)."""

    outcome: str
    previous: Optional[str] = None

    @property
    def changed(self) -> bool:
        return self.outcome == CHANGED


def _now() -> datetime:
    """Naive UTC, as the tasks and process_runs columns store it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def set_task_status(
    db: AsyncSession,
    task_id: str,
    to: str,
    *,
    actor: str,
    cause: str,
    closed_reason: Optional[str] = None,
    next_check_at: Any = KEEP,
) -> Transition:
    """Move task ``task_id`` to ``to``. A terminal ``to`` clears the next check; otherwise ``next_check_at`` (a naive
    UTC datetime, or None) replaces it unless it is KEEP. ``closed_reason`` is merged into supplementary_context on a
    change only."""

    def values(prev: Optional[str], context: Any) -> dict:
        changes = {}
        if to in TERMINAL:
            changes["next_check_at"] = None
        elif next_check_at is not KEEP:
            changes["next_check_at"] = next_check_at
        if closed_reason and prev != to:
            changes["supplementary_context"] = {**(context or {}), "closed_reason": closed_reason}
        return changes

    read = (Task.status, Task.supplementary_context) if closed_reason else (Task.status,)
    return await _compare_and_set(
        db, Task, read, task_id, to, TaskStatus, TERMINAL, task_move_allowed, values,
        refused=TASK_REFUSED, metric="task_transition_refused", actor=actor, cause=cause)


async def set_run_state(
    db: AsyncSession,
    run_id: str,
    to: str,
    *,
    actor: str,
    cause: str,
    ended: bool = False,
    release_lease: bool = False,
    next_check_at: Any = KEEP,
) -> Transition:
    """Move process run ``run_id`` to ``to``. ``ended`` sets ended_at; ``release_lease`` clears lease_owner; a terminal
    ``to`` clears the next check, and otherwise ``next_check_at`` replaces it unless it is KEEP."""

    def values(_prev: Optional[str], _context: Any) -> dict:
        changes = {}
        if ended:
            changes["ended_at"] = _now()
        if release_lease:
            changes["lease_owner"] = None
        if to in RUN_TERMINAL:
            changes["next_check_at"] = None
        elif next_check_at is not KEEP:
            changes["next_check_at"] = next_check_at
        return changes

    return await _compare_and_set(
        db, ProcessRun, (ProcessRun.current_state,), run_id, to, RunState, RUN_TERMINAL, run_move_allowed, values,
        refused=RUN_REFUSED, metric="run_transition_refused", actor=actor, cause=cause)


async def _compare_and_set(
    db: AsyncSession,
    model: Any,
    read: tuple,
    row_id: str,
    to: str,
    enum: type[Enum],
    terminal: frozenset,
    allowed: Callable[[Optional[str], str], bool],
    values: Callable[[Optional[str], Any], dict],
    *,
    refused: str,
    metric: str,
    actor: str,
    cause: str,
) -> Transition:
    """Steps 1-6 of specs/WP-04.md WP04-13: read, keep a terminal value, check the move, update where the value is
    still the one read, and on a lost race read again."""
    column = read[0]
    # A member of another enum equals a value of this one, so it would pass for an unchanged terminal value; ``allowed``
    # refuses it instead.
    foreign = isinstance(to, Enum) and not isinstance(to, enum)
    prev = None
    for _attempt in range(1 + RETRIES):
        row = (await db.execute(select(*read).where(model.id == row_id))).first()
        if row is None:
            return Transition(MISSING)
        prev = row[0]
        if prev == to and to in terminal and not foreign:
            return Transition(UNCHANGED, prev)
        if not allowed(prev, to):
            return _refuse(refused, metric, row_id, prev, to, actor, cause)
        changes = values(prev, row[1] if len(row) > 1 else None)
        if prev == to and not changes:
            return Transition(UNCHANGED, prev)
        result = await db.execute(
            update(model).where(model.id == row_id, column == prev).values({column.key: to, **changes}))
        if result.rowcount:
            return Transition(CHANGED if prev != to else UNCHANGED, prev)
    return _refuse(refused, metric, row_id, prev, to, actor, "contended")


def _refuse(message: str, metric: str, row_id: str, prev: Optional[str], to: str, actor: str, cause: str) -> Transition:
    logger.warning(message, row_id, prev, to, actor, cause)
    metrics_inc(metric)
    return Transition(REFUSED, prev)
