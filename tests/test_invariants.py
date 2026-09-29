"""Each invariant fails on a seeded bad state and passes once the state is right."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app import reconciler
from app.db.models import Base, Event, MemoryItem, ProcessRun, Task, TaskRegistryEntry, VectorOutbox
from app.memory import hygiene
from app.production import canary_sentinel, invariants
from app.production.readiness import CheckResult

CANARY_GOAL = "RMP CANARY: Reply with exactly CANARY_OK on its own line. No tools."
NO_DRIFT = {"memory_missing": 0, "memory_orphans": 0, "registry_missing": 0, "registry_orphans": 0}


def ago(**delta):
    return datetime.utcnow() - timedelta(**delta)


@pytest.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'invariants.db'}")
    tables = [m.__table__ for m in (Task, Event, MemoryItem, TaskRegistryEntry, VectorOutbox, ProcessRun)]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(invariants, "AsyncSessionLocal", factory), patch.object(hygiene, "AsyncSessionLocal", factory):
        yield factory
    await engine.dispose()


async def seed(factory, *rows):
    async with factory() as db:
        db.add_all(rows)
        await db.commit()


def task(tid, status="completed", updated=None, goal="Compare visa rules", task_type="user"):
    return Task(id=tid, goal=goal, task_type=task_type, status=status, updated_at=updated or ago(minutes=10))


def event(entity_id, event_type, at):
    return Event(correlation_id=entity_id, entity_type="task", entity_id=entity_id,
                 event_type=event_type, occurred_at=at, event_payload={})


async def test_a_completed_user_task_needs_an_evaluator_accept(session):
    await seed(session, task("t1"), task("c1", goal=CANARY_GOAL, task_type="canary"))
    result = await invariants.check_judged_deliveries()
    assert result.status == "fail" and result.details["task_ids"] == ["t1"]

    await seed(session, event("t1", "evaluator.accept", ago(minutes=11)))
    assert (await invariants.check_judged_deliveries()).status == "pass"


async def test_an_attached_message_is_answered_or_resubmitted(session):
    await seed(
        session,
        task("dropped"), event("dropped", "evaluator.accept", ago(minutes=30)),
        event("dropped", "intake.attach", ago(minutes=20)),
        task("answered"), event("answered", "intake.attach", ago(minutes=20)),
        event("answered", "evaluator.accept", ago(minutes=15)),
        task("stopped", status="stopped_by_user"), event("stopped", "intake.attach", ago(minutes=20)),
        task("live", status="running"), event("live", "intake.attach", ago(minutes=20)),
    )
    result = await invariants.check_attached_messages()
    assert result.status == "fail" and result.details["task_ids"] == ["dropped"]

    await seed(session, event("dropped", "task.messages_resubmitted", ago(minutes=9)))
    assert (await invariants.check_attached_messages()).status == "pass"


async def test_a_run_that_just_ended_gets_time_to_resubmit(session):
    await seed(session, task("t1", updated=ago(seconds=30)), event("t1", "intake.attach", ago(minutes=1)))
    assert (await invariants.check_attached_messages()).status == "pass"


async def test_a_slack_delivery_failure_fails_for_a_day(session):
    await seed(session, event("t1", "slack.delivery_failed", ago(hours=25)))
    assert (await invariants.check_slack_delivery()).status == "pass"

    await seed(session, event("t2", "slack.delivery_failed", ago(hours=1)))
    result = await invariants.check_slack_delivery()
    assert result.status == "fail" and result.details["task_ids"] == ["t2"]


async def test_an_orphan_recovery_warns(session):
    assert (await invariants.check_orphan_recoveries()).status == "pass"
    await seed(session, event("t1", "reconciler.orphan_reply_rejudged", ago(hours=2)))
    result = await invariants.check_orphan_recoveries()
    assert result.status == "warn" and result.details == {"reconciler.orphan_reply_rejudged": 1}


def memory(mid, content, source="t1", scope_type="user", scope_id="default"):
    return MemoryItem(id=mid, scope_type=scope_type, scope_id=scope_id, memory_type="semantic",
                      content=content, provenance_ref={"task_id": source})


async def test_internal_runs_leave_no_trace_in_shared_memory(session):
    await seed(session, task("t1"), task("c1", goal=CANARY_GOAL, task_type="canary"),
               memory("m-ok", "Kirill asked why the canary sentinel paged him."))
    assert (await invariants.check_memory_hygiene()).status == "pass"

    await seed(session, memory("m-canary", "The reply was short.", source="c1"),
               memory("m-quote", "Aura said CANARY_OK."),
               TaskRegistryEntry(id="r1", task_id="c1", intent_snippet=CANARY_GOAL))
    result = await invariants.check_memory_hygiene()
    assert result.status == "fail"
    assert result.details == {"memory_ids": ["m-canary", "m-quote"], "registry_task_ids": ["c1"]}


async def _vector_check(drift=NO_DRIFT):
    with patch.object(invariants, "is_vector_memory_enabled", return_value=True), \
         patch("app.memory.vector_sync.reconcile", AsyncMock(return_value=dict(drift))):
        return await invariants.check_vector_sync()


async def test_vectors_warn_on_drift_and_fail_when_the_outbox_stops(session):
    assert (await _vector_check()).status == "pass"
    assert (await _vector_check({**NO_DRIFT, "memory_missing": 2})).status == "warn"

    await seed(session, VectorOutbox(kind="memory", ref_id="m1", next_attempt_at=ago(minutes=20)))
    result = await _vector_check()
    assert result.status == "fail" and result.details["outbox_overdue"] == 1


async def test_a_row_the_outbox_keeps_failing_on_fails(session):
    await seed(session, VectorOutbox(kind="registry", ref_id="t1", attempts=5, last_error="embed 500",
                                     next_attempt_at=datetime.utcnow() + timedelta(minutes=30)))
    result = await _vector_check()
    assert result.status == "fail" and "registry/t1: embed 500" in result.message


async def test_a_check_that_cannot_run_reports_instead_of_breaking_readiness():
    async def check_boom():
        raise RuntimeError("database down")

    result = await invariants._run(check_boom)
    assert (result.name, result.status) == ("boom", "warn") and "database down" in result.message


async def _sentinel_pass(tmp_path, checks):
    with patch.object(canary_sentinel, "ALERT_STATE_PATH", tmp_path / "alerts.json"), \
         patch("app.production.invariants.run_invariant_checks", AsyncMock(return_value=checks)), \
         patch.object(canary_sentinel, "notify_ops_slack", new_callable=AsyncMock, return_value=True) as notify, \
         patch.object(canary_sentinel, "send_alert", new_callable=AsyncMock, return_value=True):
        first = await canary_sentinel._check_invariants("test")
        second = await canary_sentinel._check_invariants("test")
    return first, second, notify


async def test_a_broken_invariant_alerts_the_operator_once_per_cooldown(tmp_path):
    checks = [
        CheckResult("slack_delivery", "fail", "1 Slack delivery failure(s) in 24h", {"task_ids": ["abcdef123456"]}),
        CheckResult("orphan_recoveries", "warn", "1 run(s) died after Aura answered", {}),
    ]
    first, second, notify = await _sentinel_pass(tmp_path, checks)
    assert first["alerted"] is True and [b["name"] for b in first["broken"]] == ["slack_delivery"]
    assert second["alert_suppressed"] == "cooldown"
    notify.assert_awaited_once()
    message = notify.await_args.args[0]
    assert "slack_delivery: 1 Slack delivery failure(s) in 24h (abcdef12)" in message
    assert "orphan_recoveries" not in message


async def test_warnings_alone_do_not_page(tmp_path):
    first, _, notify = await _sentinel_pass(tmp_path, [CheckResult("vector_sync", "warn", "2 out of sync", {})])
    assert first == {"broken": [], "alerted": False}
    notify.assert_not_awaited()


async def test_runs_of_ended_tasks_are_closed(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[Task.__table__, ProcessRun.__table__, Event.__table__])
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await seed(
        factory,
        task("failed", status="failed"), task("done", status="completed"), task("live", status="running"),
        ProcessRun(id="r-failed", task_id="failed", current_state="running", next_check_at=ago(minutes=1)),
        ProcessRun(id="r-done", task_id="done", current_state="blocked"),
        ProcessRun(id="r-live", task_id="live", current_state="running"),
        ProcessRun(id="r-old", task_id="failed", current_state="superseded"),
    )
    now = datetime.utcnow()
    async with factory() as db:
        assert await reconciler.close_runs_of_ended_tasks(db, now) == 2
        await db.commit()
    async with factory() as db:
        runs = {r.id: r for r in (await db.execute(select(ProcessRun))).scalars()}
        closed = (await db.execute(select(Event.entity_id).where(
            Event.event_type == "reconciler.process_run_closed"))).scalars().all()
    await engine.dispose()
    assert (runs["r-failed"].current_state, runs["r-failed"].ended_at, runs["r-failed"].next_check_at) == (
        "failed_terminal", now, None)
    assert runs["r-done"].current_state == "completed"
    assert runs["r-live"].current_state == "running" and runs["r-old"].current_state == "superseded"
    assert sorted(closed) == ["r-done", "r-failed"]
