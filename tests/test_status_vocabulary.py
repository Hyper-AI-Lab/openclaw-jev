"""The status vocabulary (app/status.py), its named sets, and the guard that keeps every task, run and step status
literal in it, workflow code included."""
import ast
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from temporalio.converter import DataConverter
from temporalio.worker.workflow_sandbox import SandboxRestrictions
from temporalio.worker.workflow_sandbox._importer import Importer
from temporalio.worker.workflow_sandbox._restrictions import RestrictionContext

import app.status
from app.activities import db_activities
from app.db.models import Base, ProcessRun, Task
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

REPO = Path(__file__).resolve().parents[1]
ENUMS = (TaskStatus, RunState, StepStatus)
VOCABULARY = frozenset(member.value for enum in ENUMS for member in enum)


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


async def test_a_workflow_payload_encodes_its_members_as_their_strings():
    """Temporal's converter sends the same bytes when workflow code names a status by its member (WP04-09)."""
    members = [m for enum in ENUMS for m in enum]
    as_members = {"task_id": "t1", "status": TaskStatus.CANCELLED, "state": RunState.CANCELED, "all": members}
    as_strings = {"task_id": "t1", "status": "cancelled", "state": "canceled", "all": [m.value for m in members]}
    encoded = await DataConverter.default.encode([as_members, TaskStatus.FAILED])
    assert encoded == await DataConverter.default.encode([as_strings, "failed"])


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


@pytest.fixture
async def runs(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Task.__table__, ProcessRun.__table__])
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(db_activities, "AsyncSessionLocal", factory):
        yield factory
    await engine.dispose()


async def test_a_task_spelling_is_stored_as_the_run_spelling(runs):
    async with runs() as db:
        db.add_all([Task(id="t1", goal="g", status="running"),
                    ProcessRun(id="r1", task_id="t1", current_state="awaiting_approval")])
        await db.commit()

    assert await db_activities.update_process_state(
        {"process_run_id": "r1", "state": "cancelled", "ended": True, "actor": "test"})

    async with runs() as db:
        run = (await db.execute(select(ProcessRun).where(ProcessRun.id == "r1"))).scalar_one()
    assert run.current_state == "canceled" and run.ended_at is not None


async def test_a_call_without_a_state_keeps_the_stored_spelling(runs):
    """Only a state handed in is mapped: a run already stored with a task spelling is not rewritten (rule 16)."""
    async with runs() as db:
        db.add_all([Task(id="t3", goal="g", status="cancelled"),
                    ProcessRun(id="r3", task_id="t3", current_state="cancelled", lease_owner="worker-1")])
        await db.commit()

    assert await db_activities.update_process_state({"process_run_id": "r3", "release_lease": True, "actor": "test"})

    async with runs() as db:
        run = (await db.execute(select(ProcessRun).where(ProcessRun.id == "r3"))).scalar_one()
    assert run.current_state == "cancelled" and run.lease_owner is None


async def test_a_cancelled_close_clears_the_next_check(runs):
    async with runs() as db:
        next_check_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=60)
        db.add(Task(id="t2", goal="g", status="running", next_check_at=next_check_at))
        await db.commit()

    with patch("app.task_registry.hooks.index_terminal_task_async", new_callable=AsyncMock) as index:
        assert await db_activities.update_task_status({"task_id": "t2", "status": "cancelled", "actor": "test"})

    async with runs() as db:
        task = await db.get(Task, "t2")
    assert task.status == "cancelled" and task.next_check_at is None
    index.assert_awaited_once_with("t2")


# A SQL text that sets a status column to a literal where that column holds another literal: a rename (rule 16).
RENAME = re.compile(
    r"\bset\s+(?:\w+\.)?(status|current_state)\s*=\s*'[^']*'.*\bwhere\b.*\b\1\s*(?:=|<>|!=|\bin\b)\s*\(?\s*'",
    re.IGNORECASE | re.DOTALL)


def _python_files(*roots):
    for root in roots:
        if root.is_file():
            yield root
        elif root.is_dir():
            yield from sorted(root.rglob("*.py"))


def _parsed(*roots):
    """(repo-relative path, tree) of every Python file under ``roots``; files that do not parse are skipped
    (ops/migrate_task_registry_schema.py is bash)."""
    for path in _python_files(*roots):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        yield path.relative_to(REPO).as_posix(), tree


def test_no_stored_status_is_renamed():
    assert RENAME.search("UPDATE process_runs SET current_state = 'cancelled' WHERE current_state = 'canceled'")
    assert RENAME.search("update tasks set status='failed'\n where id = :id and status in ('running', 'created')")
    assert not RENAME.search("UPDATE tasks SET status = :to WHERE id = :id AND status IN :active")
    assert not RENAME.search("UPDATE tasks SET status = 'failed' WHERE id = :id")
    renames = [f"{rel}:{node.lineno}" for rel, tree in _parsed(REPO / "app", REPO / "ops", REPO / "migrations")
               for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str) and RENAME.search(node.value)]
    assert not renames, "SQL that renames a stored status:\n" + "\n".join(renames)


# The guard (specs/WP-04.md D-4). A vocabulary value is any TaskStatus, RunState or StepStatus value. The scanners'
# track statuses are not task statuses and retire with the scanners (K-31).
GUARDED = ("app", "ops", "worker.py")
EXEMPT = ("app/status.py", "app/scanners/")
WORKFLOW_CODE = "app/workflows/"
ROWS = {"Task": "status", "ProcessRun": "current_state", "Step": "status"}
STATUS_ATTRIBUTES = frozenset({"status", "current_state"})
COMPARISONS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)
# .in_() and SQLAlchemy's two spellings of its negation, .notin_() and 2.0's .not_in().
MEMBERSHIP_CALLS = ("in_", "notin_", "not_in")
COLLECTIONS = (ast.Set, ast.Tuple, ast.List)
STATUS_WORD = re.compile(r"status|current_state", re.IGNORECASE)
QUOTED_VALUE = re.compile("'(?:" + "|".join(sorted(VOCABULARY)) + ")'")
# G7's contexts in workflow code: the activities whose payload carries a status, the calls that schedule them and
# the payload keys that hold it, and the workflows' own status helpers.
STATUS_ACTIVITIES = frozenset({
    "update_task_status", "update_process_state", "finalize_task_failure", "execute_compensation", "record_step"})
ACTIVITY_CALLS = frozenset({"execute_activity", "start_activity"})
STATUS_KEYS = frozenset({"status", "task_status", "state", "process_state"})
STATUS_HELPERS = frozenset({"_status", "_close", "_ask", "_stop_task"})
ENUM_NAMES = frozenset(enum.__name__ for enum in ENUMS)


def _string(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _vocabulary_set(node):
    """G1: a set, tuple or list literal of two or more strings, every one a vocabulary value."""
    return (isinstance(node, COLLECTIONS) and len(node.elts) >= 2
            and all(_string(e) and e.value in VOCABULARY for e in node.elts))


def _holds_a_string(node):
    return _string(node) or (isinstance(node, COLLECTIONS) and any(_string(e) for e in node.elts))


def _all_vocabulary(node):
    """A string constant, or a literal collection of them, whose strings are all vocabulary values."""
    values = [node] if _string(node) else list(node.elts) if isinstance(node, COLLECTIONS) and node.elts else None
    return values is not None and all(_string(v) and v.value in VOCABULARY for v in values)


def _column(node):
    """Task.status, ProcessRun.current_state or Step.status."""
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and ROWS.get(node.value.id) == node.attr


def _status_attribute(node):
    return isinstance(node, ast.Attribute) and node.attr in STATUS_ATTRIBUTES


def _leaves(node):
    """The expressions a value can evaluate to: itself, or both branches of a conditional."""
    if isinstance(node, ast.IfExp):
        return _leaves(node.body) + _leaves(node.orelse)
    return [node]


def _assigned_values(node):
    """The constants an assigned expression can evaluate to: itself, or both branches of a conditional."""
    return [leaf.value for leaf in _leaves(node) if _string(leaf)]


def _imports_a_row(tree):
    return any(isinstance(node, ast.ImportFrom) and node.module == "app.db.models"
               and any(alias.name in ROWS for alias in node.names) for node in ast.walk(tree))


def _model_defaults(tree):
    """G5 in app/db/models.py: a string-constant default= of Task.status, ProcessRun.current_state or Step.status."""
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or cls.name not in ROWS:
            continue
        for stmt in cls.body:
            if (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)
                    and any(isinstance(t, ast.Name) and t.id == ROWS[cls.name] for t in stmt.targets)
                    and any(k.arg == "default" and _string(k.value) for k in stmt.value.keywords)):
                yield stmt.lineno


def _vocabulary_value(node):
    return _string(node) and node.value in VOCABULARY


def status_member(node):
    """An app.status member written out: ``TaskStatus.RUNNING``."""
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ENUM_NAMES


def _key(node):
    return node.value if _string(node) else None


def _called(call):
    """``f`` for ``f(...)`` and ``x.f(...)``."""
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _status_read(node):
    """The name ``status``, ``<x>.get("status")`` or ``<x>["status"]``."""
    if isinstance(node, ast.Name):
        return node.id == "status"
    if isinstance(node, ast.Call):
        return _called(node) == "get" and bool(node.args) and _key(node.args[0]) == "status"
    return isinstance(node, ast.Subscript) and _key(node.slice) == "status"


def _activity_payload(call):
    """The dict a status activity is given: ``workflow.execute_activity(<status activity>, {...}, ...)``."""
    func, args = call.func, call.args
    if (isinstance(func, ast.Attribute) and func.attr in ACTIVITY_CALLS and isinstance(func.value, ast.Name)
            and func.value.id == "workflow" and len(args) >= 2 and isinstance(args[0], ast.Name)
            and args[0].id in STATUS_ACTIVITIES and isinstance(args[1], ast.Dict)):
        return args[1]
    return None


def workflow_status_values(tree, is_status):
    """G7 (workflow code): (clause, node) for every value ``is_status`` accepts in a status context.

    ``is_status`` picks the values reported: a vocabulary string for the guard, an app.status member for the
    lifecycle test (tests/test_lifecycles.py). A conditional expression counts by each of its branches.
    """
    found = []

    def take(clause, node):
        found.extend((clause, leaf) for leaf in _leaves(node) if is_status(leaf))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            payload = _activity_payload(node)
            if payload is not None:
                for key, value in zip(payload.keys, payload.values):
                    if _key(key) in STATUS_KEYS:
                        take("G7a", value)
            if _called(node) in STATUS_HELPERS:
                for arg in [*node.args, *(keyword.value for keyword in node.keywords)]:
                    take("G7c", arg)
        if isinstance(node, ast.Dict):
            entries = {_key(key): value for key, value in zip(node.keys, node.values)}
            if "status" in entries and ("task_id" in entries or "step_id" in entries):
                take("G7b", entries["status"])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in STATUS_HELPERS:
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    take("G7c", default)
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for op, left, right in zip(node.ops, operands, operands[1:]):
                if not isinstance(op, COMPARISONS):
                    continue
                for side, other in ((left, right), (right, left)):
                    compared = list(other.elts) if isinstance(other, COLLECTIONS) else [other]
                    # Strings and members alike make a status comparison, so a half-converted tuple shows its strings.
                    if (_status_read(side) and compared
                            and all(_vocabulary_value(value) or status_member(value) for value in compared)):
                        found.extend(("G7d", value) for value in compared if is_status(value))
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id.endswith("status") for target in targets):
                take("G7e", node.value)
    return found


# The enum each status slot of workflow code holds: by status activity and payload key, and by the name of a status
# helper's parameter. The value cannot tell them apart: StepStatus.RUNNING equals TaskStatus.RUNNING.
ACTIVITY_SLOTS = {
    "update_task_status": {"status": TaskStatus},
    "finalize_task_failure": {"task_status": TaskStatus, "process_state": RunState},
    "execute_compensation": {},
    "update_process_state": {"state": RunState},
    "record_step": {"status": StepStatus},
}
HELPER_SLOTS = {"task_status": TaskStatus, "status": TaskStatus, "process_state": RunState}


def helper_parameters(tree):
    """The positional parameters after ``self`` of each status helper defined in ``tree``, by helper name."""
    return {node.name: [arg.arg for arg in [*node.args.posonlyargs, *node.args.args][1:]]
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in STATUS_HELPERS}


def status_slots(tree, helpers):
    """(enum, member) for each app.status member written into a status slot of workflow code: a status activity's
    payload, a status helper's argument or parameter default (``helpers`` gives their positional parameters), or the
    status of a task's or a step's dict. ``enum`` is None for a slot ACTIVITY_SLOTS and HELPER_SLOTS do not name."""
    found = []

    def take(enum, node):
        found.extend((enum, leaf) for leaf in _leaves(node) if status_member(leaf))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            payload = _activity_payload(node)
            if payload is not None:
                for key, value in zip(payload.keys, payload.values):
                    if _key(key) in STATUS_KEYS:
                        take(ACTIVITY_SLOTS[node.args[0].id].get(_key(key)), value)
            if _called(node) in STATUS_HELPERS:
                parameters = helpers.get(_called(node), [])
                for position, arg in enumerate(node.args):
                    take(HELPER_SLOTS.get(parameters[position]) if position < len(parameters) else None, arg)
                for keyword in node.keywords:
                    take(HELPER_SLOTS.get(keyword.arg), keyword.value)
        if isinstance(node, ast.Dict):
            entries = {_key(key): value for key, value in zip(node.keys, node.values)}
            if "status" in entries and ("task_id" in entries or "step_id" in entries):
                take(TaskStatus if "task_id" in entries else StepStatus, entries["status"])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in STATUS_HELPERS:
            positional = [*node.args.posonlyargs, *node.args.args]
            defaulted = zip(positional[len(positional) - len(node.args.defaults):], node.args.defaults)
            for arg, default in [*defaulted, *zip(node.args.kwonlyargs, node.args.kw_defaults)]:
                if default is not None:
                    take(HELPER_SLOTS.get(arg.arg), default)
    return found


def violations(tree, path):
    """Sorted (line, rule) pairs of G1-G6 in one module, and of G7 when it is workflow code."""
    found = set()
    imports_a_row = _imports_a_row(tree)
    for node in ast.walk(tree):
        if _vocabulary_set(node):
            found.add((node.lineno, "G1"))
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for op, left, right in zip(node.ops, operands, operands[1:]):
                if not isinstance(op, COMPARISONS):
                    continue
                for side, other in ((left, right), (right, left)):
                    if _column(side) and _holds_a_string(other):
                        found.add((node.lineno, "G2"))
                    if imports_a_row and _status_attribute(side) and _all_vocabulary(other):
                        found.add((node.lineno, "G3"))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MEMBERSHIP_CALLS
                and _column(node.func.value) and any(_holds_a_string(arg) for arg in node.args)):
            found.add((node.lineno, "G2"))
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                pairs = [(target, node.value)]
                if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    pairs = list(zip(target.elts, node.value.elts))
                for name, value in pairs:
                    if (value is not None and _status_attribute(name)
                            and any(v in VOCABULARY for v in _assigned_values(value))):
                        found.add((node.lineno, "G4"))
        if isinstance(node, ast.Call):
            row = _called(node)
            if row in ROWS and any(k.arg in STATUS_ATTRIBUTES and _string(k.value) for k in node.keywords):
                found.add((node.lineno, "G5"))
        if _string(node) and STATUS_WORD.search(node.value) and QUOTED_VALUE.search(node.value):
            found.add((node.lineno, "G6"))
    if path == "app/db/models.py":
        found.update((line, "G5") for line in _model_defaults(tree))
    if path.startswith(WORKFLOW_CODE):
        found.update((value.lineno, clause) for clause, value in workflow_status_values(tree, _vocabulary_value))
    return sorted(found)


def _rules(source, path="app/example.py"):
    return {rule for _, rule in violations(ast.parse(source), path)}


@pytest.mark.parametrize("rule, source, path", [
    ("G1", 'DONE = frozenset({"completed", "failed"})', "app/example.py"),
    ("G1", 'if run.current_state in ("canceled", "superseded"):\n    pass', "app/example.py"),
    ("G2", 'select(Task).where(Task.status == "running")', "app/example.py"),
    ("G2", 'select(Step).where(Step.status.in_(["running", "pending"]))', "app/example.py"),
    ("G2", 'select(ProcessRun).where(ProcessRun.current_state.notin_(("canceled",)))', "app/example.py"),
    ("G2", 'select(Task).where(Task.status.not_in(("canceled",)))', "app/example.py"),
    ("G3", 'from app.db.models import Task\nif task.status == "completed":\n    pass', "app/example.py"),
    ("G3", 'from app.db.models import Step\nok = step.status != "pending"', "app/example.py"),
    ("G4", 'task.status = "failed"', "app/example.py"),
    ("G4", 'run.current_state = "canceled" if stopped else "completed"', "app/example.py"),
    ("G5", 'db.add(Task(id=tid, goal=goal, status="created"))', "app/example.py"),
    ("G5", 'class ProcessRun(Base):\n    current_state = Column(String, default="created")', "app/db/models.py"),
    ("G6", 'text("SELECT id FROM tasks WHERE status = \'running\'")', "app/example.py"),
    ("G6", 'sql = "UPDATE process_runs SET current_state = \'canceled\' WHERE id = :id"', "ops/example.py"),
    ("G7a", 'workflow.execute_activity(update_process_state, {"process_run_id": run_id, "state": "awaiting_approval"})',
     "app/workflows/example.py"),
    ("G7a", 'workflow.start_activity(finalize_task_failure, {"process_run_id": run_id,\n'
            '    "process_state": "failed_terminal" if hard else "compensated"})', "app/workflows/example.py"),
    ("G7b", 'result = {"status": "compensated", "task_id": task_id, "step": step.name}', "app/workflows/example.py"),
    ("G7b", 'return {"status": "pending", "reason": why, "text": "", "step_id": step_id}', "app/workflows/example.py"),
    ("G7c", 'await self._close("cancelled", "canceled")', "app/workflows/example.py"),
    ("G7c", 'async def _stop_task(self, task_id, status="stopped_by_user"):\n    pass', "app/workflows/example.py"),
    ("G7d", 'if result.get("status") != "completed":\n    pass', "app/workflows/example.py"),
    ("G7d", 'if quality["status"] in ("failed", "stopped_by_user"):\n    pass', "app/workflows/example.py"),
    # Half converted: a member next to a string does not hide the string.
    ("G7d", 'if status in (StepStatus.FAILED, "blocked"):\n    pass', "app/workflows/example.py"),
    ("G7e", 'step_status = "completed" if done else ("failed" if broken else decision["status"])',
     "app/workflows/example.py"),
])
def test_each_rule_flags_its_snippet(rule, source, path):
    assert rule in _rules(source, path)


@pytest.mark.parametrize("rule, source, path", [
    ("G1", 'SOFT_HEALTH_STATUSES = frozenset({"timeout", "failed"})', "app/production/canary_sentinel.py"),
    ("G2", "select(Task).where(Task.status.in_(TERMINAL), Task.status != TaskStatus.CREATED)", "app/example.py"),
    ("G3", 'from app.db.models import Task\nif result.status in ("failed", "timeout"):\n    pass', "app/example.py"),
    ("G4", 'result = {"status": "failed", "summary": "main moved while the deploy waited"}', "app/coding/deploy.py"),
    ("G5", 'db.add(DeepContextReport(id=rid, status="failed"))', "app/activities/deep_memory_activities.py"),
    ("G6", 'text("SELECT count(*) FROM tasks WHERE status IN :statuses")', "app/production/canary_sentinel.py"),
    # A deep-recall report's status, sent to an activity that writes no task, run or step status.
    ("G7a", 'workflow.execute_activity(settle_recall_report, {"report_id": rid, "status": "cancelled"})',
     "app/workflows/recall_phase.py"),
    # A deploy result names no task or step.
    ("G7b", 'shipped = {"status": "failed", "summary": "Shipping failed; nothing was deployed."}',
     "app/workflows/coding_task.py"),
    ("G7c", 'await self._settle_recall("none", status="cancelled", reason=reason)', "app/workflows/recall_phase.py"),
    ("G7d", 'if report.get("status") in ("failed", "timeout"):\n    pass', "app/workflows/example.py"),
    ("G7d", 'if result.get("status") in (TaskStatus.FAILED, TaskStatus.COMPENSATED):\n    pass',
     "app/workflows/example.py"),
    # The default of a read is no write.
    ("G7e", 'status = result.get("status", "pending")', "app/workflows/catalog_task.py"),
    # G7 covers workflow code only.
    ("G7b", 'return {"status": "cancelled", "task_id": task_id}', "app/api/server.py"),
])
def test_each_rule_passes_its_negative(rule, source, path):
    assert not _rules(source, path)


def test_no_status_literal_outside_the_vocabulary():
    modules = [(rel, tree) for rel, tree in _parsed(*(REPO / root for root in GUARDED)) if not rel.startswith(EXEMPT)]
    # The walk parsed a module under every guarded root: a walk that finds no file would pass on anything.
    unreached = [root for root in GUARDED if not any(rel == root or rel.startswith(root + "/") for rel, _ in modules)]
    assert not unreached, f"the guard parsed no module under {unreached}"
    found = [f"{rel}:{line} {rule}" for rel, tree in modules for line, rule in violations(tree, rel)]
    assert not found, "task, run or step status literals outside app/status.py:\n" + "\n".join(found)


# G8 (WP04-16): app/transitions.py is the one writer of tasks.status and process_runs.current_state.
STATUS_WRITER = "app/transitions.py"
TRANSITION_ROWS = frozenset({"Task", "ProcessRun"})
RAW_STATUS_UPDATE = re.compile(r"\bupdate\s+(tasks|process_runs)\b", re.IGNORECASE)
# Reviewed writes of another row's status in a module that imports Task or ProcessRun, as (path, function).
OTHER_ROW_STATUS_WRITERS = frozenset({
    ("app/activities/db_activities.py", "record_step"),  # Step
    ("app/activities/db_activities.py", "execute_compensation"),  # Step
    ("app/deep_memory/enrich.py", "enrich_document"),  # DeepDocument
})
# This batch only: the sentinel's cancel is already a compare-and-set in SQL; batch 4 replaces it, and this entry goes.
RAW_STATUS_UPDATERS = frozenset({("app/production/canary_sentinel.py", "cancel_task_sync")})
# G9 (WP04-18): the event log is immutable (D-24). WP-27's K-12 retention job adds itself here, the one deleter.
EVENT_DELETERS = frozenset()
RAW_EVENT_WRITE = re.compile(r"\b(delete\s+from|update|truncate(\s+table)?)\s+events\b", re.IGNORECASE)


def _nodes_by_function(tree):
    """(node, name of the innermost function around it, or None) for every node of ``tree``."""
    stack = [(tree, None)]
    while stack:
        node, function = stack.pop()
        yield node, function
        inner = node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
        stack.extend((child, inner) for child in ast.iter_child_nodes(node))


def _sqlalchemy_names(tree, name):
    """The local names of SQLAlchemy's ``name`` (``update``, ``delete``), aliases included."""
    return {name} | {alias.asname for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy")
                     for alias in node.names if alias.name == name and alias.asname}


def _statement_on(call, names, rows):
    """``update(Task)`` (or ``delete``; ``names`` holds the statement's local names) or ``Task.__table__.update()``."""
    func = call.func
    if (isinstance(func, ast.Name) and func.id in names and call.args and isinstance(call.args[0], ast.Name)
            and call.args[0].id in rows):
        return True
    table = func.value if isinstance(func, ast.Attribute) else None
    return (isinstance(table, ast.Attribute) and func.attr in names and table.attr == "__table__"
            and isinstance(table.value, ast.Name) and table.value.id in rows)


def _assigned_attributes(node):
    """The attribute targets of an assignment, tuple targets unpacked."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    flat = []
    while targets:
        target = targets.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            targets.extend(target.elts)
        elif isinstance(target, ast.Attribute):
            flat.append(target)
    return flat


def writer_violations(tree, path):
    """Sorted (line, "G8") pairs: a status written outside app/transitions.py."""
    if path == STATUS_WRITER:
        return []
    updates = _sqlalchemy_names(tree, "update")
    imports_a_row = any(isinstance(node, ast.ImportFrom) and node.module == "app.db.models"
                        and any(alias.name in TRANSITION_ROWS for alias in node.names) for node in ast.walk(tree))
    found = set()
    for node, function in _nodes_by_function(tree):
        if isinstance(node, ast.Call) and _statement_on(node, updates, TRANSITION_ROWS):
            found.add((node.lineno, "G8"))
        if _string(node) and RAW_STATUS_UPDATE.search(node.value) and (path, function) not in RAW_STATUS_UPDATERS:
            found.add((node.lineno, "G8"))
        if (imports_a_row and isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                and any(_status_attribute(target) for target in _assigned_attributes(node))
                and (path, function) not in OTHER_ROW_STATUS_WRITERS):
            found.add((node.lineno, "G8"))
    return sorted(found)


def _selects_events(node):
    return any(isinstance(call, ast.Call) and _called(call) == "select"
               and any(isinstance(arg, ast.Name) and arg.id == "Event" for arg in call.args)
               for call in ast.walk(node))


def _deleted_event_rows(function):
    """Lines of ``.delete(`` on a name bound, in ``function``, from ``select(Event)`` or from such a name."""
    bound, grew = set(), True
    while grew:
        grew = False
        for node in ast.walk(function):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                source, targets = node.value, node.targets if isinstance(node, ast.Assign) else [node.target]
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                source, targets = node.iter, [node.target]
            else:
                continue
            if _selects_events(source) or any(isinstance(n, ast.Name) and n.id in bound for n in ast.walk(source)):
                names = {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)} - bound
                grew |= bool(names)
                bound |= names
    return [node.lineno for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "delete"
            and any(isinstance(n, ast.Name) and n.id in bound for n in [node.func.value, *node.args])]


def event_log_violations(tree, path):
    """Sorted (line, "G9") pairs: an update or delete of the event log."""
    statements = _sqlalchemy_names(tree, "update") | _sqlalchemy_names(tree, "delete")
    found = set()
    for node, function in _nodes_by_function(tree):
        if (path, function) in EVENT_DELETERS:
            continue
        if isinstance(node, ast.Call) and _statement_on(node, statements, {"Event"}):
            found.add((node.lineno, "G9"))
        if _string(node) and RAW_EVENT_WRITE.search(node.value):
            found.add((node.lineno, "G9"))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (path, node.name) not in EVENT_DELETERS:
            found.update((line, "G9") for line in _deleted_event_rows(node))
    return sorted(found)


@pytest.mark.parametrize("source, path", [
    ("from sqlalchemy import update\nawait db.execute(update(Task).where(Task.id == tid).values(status=to))",
     "app/example.py"),
    ("from sqlalchemy import update as sa_update\nstmt = sa_update(ProcessRun).values(current_state=state)",
     "ops/example.py"),
    ("stmt = Task.__table__.update().values(status=to)", "app/example.py"),
    ('await db.execute(text("UPDATE tasks SET status = :to WHERE id = :id"))', "app/example.py"),
    ('sql = "update process_runs set current_state = :s"', "worker.py"),
    ("from app.db.models import Task\ntask.status = TaskStatus.FAILED", "app/example.py"),
    ("from app.db.models import ProcessRun\nrun.current_state, run.ended_at = state, now", "app/example.py"),
    # A reviewed writer of a step's status in one function is no licence for another.
    ("from app.db.models import Step, Task\nasync def other(step):\n    step.status = StepStatus.FAILED",
     "app/activities/db_activities.py"),
    ('def cancel_task_sync():\n    pass\nSQL = "UPDATE tasks SET status = :cancelled WHERE id = :id"',
     "app/production/canary_sentinel.py"),
])
def test_the_writer_guard_flags_its_snippet(source, path):
    assert writer_violations(ast.parse(source), path)


@pytest.mark.parametrize("source, path", [
    ("from sqlalchemy import update\nawait db.execute(update(Task).where(Task.id == tid).values(status=to))",
     "app/transitions.py"),
    ("from sqlalchemy import update\nawait db.execute(update(VectorOutbox).values(done_at=now))", "app/example.py"),
    ("from app.db.models import Task\ntask.supplementary_context = ctx", "app/example.py"),
    # A module without Task or ProcessRun writes other rows' statuses: a deep-recall report.
    ("from app.db.models import DeepContextReport\nrow.status = payload['status']", "app/example.py"),
    ("from app.db.models import Step, Task\nasync def record_step(payload):\n    existing.status = payload['ok']",
     "app/activities/db_activities.py"),
    ('def cancel_task_sync():\n    sql = "UPDATE tasks SET status = :cancelled WHERE id = :id AND status IN :active"',
     "app/production/canary_sentinel.py"),
    ('text("SELECT id FROM tasks WHERE status IN :statuses")', "app/example.py"),
])
def test_the_writer_guard_passes_its_negative(source, path):
    assert not writer_violations(ast.parse(source), path)


@pytest.mark.parametrize("source", [
    "from sqlalchemy import delete\nawait db.execute(delete(Event).where(Event.occurred_at < cutoff))",
    "from sqlalchemy import update\nawait db.execute(update(Event).values(event_payload={}))",
    "stmt = Event.__table__.delete()",
    'await db.execute(text("DELETE FROM events WHERE occurred_at < :cutoff"))',
    'sql = "TRUNCATE TABLE events"',
    'sql = "update events set event_payload = null"',
    "async def prune(db):\n    rows = await db.execute(select(Event).where(Event.occurred_at < cutoff))\n"
    "    for row in rows.scalars():\n        await db.delete(row)",
    "async def prune(db):\n    old = (await db.execute(select(Event))).scalars().all()\n"
    "    for event in [e for e in old if e.entity_id]:\n        await db.delete(event)",
])
def test_the_event_log_guard_flags_its_snippet(source):
    assert event_log_violations(ast.parse(source), "app/example.py")


@pytest.mark.parametrize("source", [
    "db.add(Event(entity_id=tid, event_type='task.cancelled'))",
    "from sqlalchemy import delete\nawait db.execute(delete(VectorOutbox).where(VectorOutbox.done_at < cutoff))",
    'sql = "SELECT * FROM events WHERE entity_id = :id"',
    # A deleted row that was not read from the event log.
    "async def prune(db):\n    events = await db.execute(select(Event))\n"
    "    stale = await db.execute(select(DeepChunk))\n    for row in stale.scalars():\n        await db.delete(row)",
])
def test_the_event_log_guard_passes_its_negative(source):
    assert not event_log_violations(ast.parse(source), "app/example.py")


def test_no_status_write_outside_the_writer_and_no_event_rewrite():
    """G8 and G9 over the guarded roots: app/transitions.py writes every status, and nothing updates or deletes an
    event. app/scanners/ is exempt (D-4): its track statuses retire with the scanners."""
    modules = [(rel, tree) for rel, tree in _parsed(*(REPO / root for root in GUARDED)) if not rel.startswith(EXEMPT)]
    assert any(rel == STATUS_WRITER for rel, _tree in modules)
    found = [f"{rel}:{line} {rule}" for rel, tree in modules
             for line, rule in [*writer_violations(tree, rel), *event_log_violations(tree, rel)]]
    assert not found, "status written outside app/transitions.py, or the event log rewritten:\n" + "\n".join(found)
    # Each reviewed exception still names a function that exists, so a stale entry is noticed and removed.
    functions = {(rel, node.name) for rel, tree in modules for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert OTHER_ROW_STATUS_WRITERS | RAW_STATUS_UPDATERS | EVENT_DELETERS <= functions


async def test_the_listener_catches_a_direct_write(runs, tmp_path, monkeypatch):
    """The runtime half of G8 (tests/conftest.py): a production module that sets a stored task's status is stopped;
    the same write through app/transitions.py, and a test seeding a row, are not."""
    import importlib.util

    from app.transitions import set_run_state, set_task_status
    from tests import conftest

    production = tmp_path / "production"
    production.mkdir()
    (production / "stray_writer.py").write_text(
        "def finish(task, run):\n    task.status = 'failed'\n\n\ndef end(run):\n    run.current_state = 'canceled'\n")
    module_spec = importlib.util.spec_from_file_location("stray_writer", production / "stray_writer.py")
    stray = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(stray)
    monkeypatch.setattr(conftest, "PRODUCTION_ROOTS", (*conftest.PRODUCTION_ROOTS, production))
    async with runs() as db:
        db.add_all([Task(id="t1", goal="g", status="running"), ProcessRun(id="r1", task_id="t1")])
        await db.commit()

        task, run = await db.get(Task, "t1"), await db.get(ProcessRun, "r1")
        with pytest.raises(AssertionError, match="^status written outside app/transitions.py$"):
            stray.finish(task, run)
        with pytest.raises(AssertionError, match="^status written outside app/transitions.py$"):
            stray.end(run)
        assert [entry.split(" ", 1)[1] for entry in conftest._STRAY_STATUS_WRITES] == [
            "Task.status = failed", "ProcessRun.current_state = canceled"]
        conftest._STRAY_STATUS_WRITES.clear()
        assert task.status == "running" and run.current_state == "created"

        assert (await set_task_status(db, "t1", TaskStatus.FAILED, actor="test", cause="listener")).changed
        assert (await set_run_state(db, "r1", RunState.FAILED_TERMINAL, actor="test", cause="listener")).changed
        task.status, run.current_state = "running", "running"
        await db.commit()
    assert conftest._STRAY_STATUS_WRITES == []
