"""The status vocabulary (app/status.py), its named sets, and the stored spellings they keep (rule 16)."""
import ast
import json
import re
from pathlib import Path
from unittest.mock import patch

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
