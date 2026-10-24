"""The status vocabulary (app/status.py), its named sets, and the guard that keeps every task, run and step status
literal outside workflow code in it."""
import ast
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
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

    assert await db_activities.update_process_state({"process_run_id": "r1", "state": "cancelled", "ended": True})

    async with runs() as db:
        run = (await db.execute(select(ProcessRun).where(ProcessRun.id == "r1"))).scalar_one()
    assert run.current_state == "canceled" and run.ended_at is not None


async def test_a_call_without_a_state_keeps_the_stored_spelling(runs):
    """Only a state handed in is mapped: a run already stored with a task spelling is not rewritten (rule 16)."""
    async with runs() as db:
        db.add_all([Task(id="t3", goal="g", status="cancelled"),
                    ProcessRun(id="r3", task_id="t3", current_state="cancelled", lease_owner="worker-1")])
        await db.commit()

    assert await db_activities.update_process_state({"process_run_id": "r3", "release_lease": True})

    async with runs() as db:
        run = (await db.execute(select(ProcessRun).where(ProcessRun.id == "r3"))).scalar_one()
    assert run.current_state == "cancelled" and run.lease_owner is None


async def test_a_cancelled_close_clears_the_next_check(runs):
    async with runs() as db:
        db.add(Task(id="t2", goal="g", status="running", next_check_at=datetime.utcnow() + timedelta(minutes=60)))
        await db.commit()

    with patch("app.task_registry.hooks.index_terminal_task_async", new_callable=AsyncMock) as index:
        assert await db_activities.update_task_status({"task_id": "t2", "status": "cancelled"})

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
# track statuses are not task statuses and retire with the scanners (K-31); app/workflows/ joins the guard with WP04-11.
GUARDED = ("app", "ops", "worker.py")
EXEMPT = ("app/status.py", "app/scanners/", "app/workflows/")
ROWS = {"Task": "status", "ProcessRun": "current_state", "Step": "status"}
STATUS_ATTRIBUTES = frozenset({"status", "current_state"})
COMPARISONS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)
COLLECTIONS = (ast.Set, ast.Tuple, ast.List)
STATUS_WORD = re.compile(r"status|current_state", re.IGNORECASE)
QUOTED_VALUE = re.compile("'(?:" + "|".join(sorted(VOCABULARY)) + ")'")


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


def _assigned_values(node):
    """The constants an assigned expression can evaluate to: itself, or both branches of a conditional."""
    if isinstance(node, ast.IfExp):
        return _assigned_values(node.body) + _assigned_values(node.orelse)
    return [node.value] if _string(node) else []


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


def violations(tree, path):
    """Sorted (line, rule) pairs of G1-G6 in one module."""
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
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("in_", "notin_")
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
            func = node.func
            row = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if row in ROWS and any(k.arg in STATUS_ATTRIBUTES and _string(k.value) for k in node.keywords):
                found.add((node.lineno, "G5"))
        if _string(node) and STATUS_WORD.search(node.value) and QUOTED_VALUE.search(node.value):
            found.add((node.lineno, "G6"))
    if path == "app/db/models.py":
        found.update((line, "G5") for line in _model_defaults(tree))
    return sorted(found)


def _rules(source, path="app/example.py"):
    return {rule for _, rule in violations(ast.parse(source), path)}


@pytest.mark.parametrize("rule, source, path", [
    ("G1", 'DONE = frozenset({"completed", "failed"})', "app/example.py"),
    ("G1", 'if run.current_state in ("canceled", "superseded"):\n    pass', "app/example.py"),
    ("G2", 'select(Task).where(Task.status == "running")', "app/example.py"),
    ("G2", 'select(Step).where(Step.status.in_(["running", "pending"]))', "app/example.py"),
    ("G2", 'select(ProcessRun).where(ProcessRun.current_state.notin_(("canceled",)))', "app/example.py"),
    ("G3", 'from app.db.models import Task\nif task.status == "completed":\n    pass', "app/example.py"),
    ("G3", 'from app.db.models import Step\nok = step.status != "pending"', "app/example.py"),
    ("G4", 'task.status = "failed"', "app/example.py"),
    ("G4", 'run.current_state = "canceled" if stopped else "completed"', "app/example.py"),
    ("G5", 'db.add(Task(id=tid, goal=goal, status="created"))', "app/example.py"),
    ("G5", 'class ProcessRun(Base):\n    current_state = Column(String, default="created")', "app/db/models.py"),
    ("G6", 'text("SELECT id FROM tasks WHERE status = \'running\'")', "app/example.py"),
    ("G6", 'sql = "UPDATE process_runs SET current_state = \'canceled\' WHERE id = :id"', "ops/example.py"),
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
])
def test_each_rule_passes_its_negative(rule, source, path):
    assert not _rules(source, path)


def test_no_status_literal_outside_the_vocabulary():
    found = [f"{rel}:{line} {rule}"
             for rel, tree in _parsed(*(REPO / root for root in GUARDED)) if not rel.startswith(EXEMPT)
             for line, rule in violations(tree, rel)]
    assert not found, "task, run or step status literals outside app/status.py:\n" + "\n".join(found)
