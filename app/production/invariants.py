"""Invariants of the Slack path, memory and delivery, read from stored facts only.

Violations count over a rolling window, so one alerts for a day and then ages out.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import List

from sqlalchemy import exists, func, select
from sqlalchemy.orm import aliased

from app.config import is_vector_memory_enabled
from app.db.database import AsyncSessionLocal
from app.deep_memory.health import INVARIANT_CHECKS as DEEP_MEMORY_INVARIANTS
from app.db.models import Event, Task, VectorOutbox
from app.memory.hygiene import canary_text_rows, internal_traces
from app.notification_policy import is_internal_task
from app.orchestrator.decision_engine import TERMINAL_STATUSES
from app.production.readiness import CheckResult
from app.reconciler import ORPHAN_EVENTS

WINDOW_HOURS = 24
# A run writes its resubmission after its terminal status.
SETTLE_MINUTES = 2
OUTBOX_OVERDUE_MINUTES = 15
OUTBOX_POISON_ATTEMPTS = 5
ANSWERED_EVENTS = ("evaluator.accept", "evaluator.escalate", "task.messages_resubmitted")
CLOSED_ON_REQUEST = ("stopped_by_user", "cancelled")


def _since() -> datetime:
    return datetime.utcnow() - timedelta(hours=WINDOW_HOURS)


async def check_judged_deliveries() -> CheckResult:
    """A user task completes only after the evaluator accepted its reply.

    A coding task whose replies the evaluator never accepted completes with RMP's own record of what shipped.
    """
    accepted = exists().where(Event.entity_id == Task.id, Event.event_type.in_(("evaluator.accept", "coding.reported_by_rmp")))
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Task.id, Task.goal, Task.task_type).where(
                    Task.status == "completed", Task.updated_at >= _since(), ~accepted
                )
            )
        ).all()
    unjudged = [r.id for r in rows if not is_internal_task(r.goal or "", r.task_type or "", [])]
    if unjudged:
        return CheckResult(
            "judged_deliveries",
            "fail",
            f"{len(unjudged)} user task(s) completed without an evaluator accept in {WINDOW_HOURS}h",
            {"task_ids": unjudged[:20]},
        )
    return CheckResult("judged_deliveries", "pass", "Every completed user task was accepted", {})


async def check_attached_messages() -> CheckResult:
    """A message attached to a running task is answered by it or sent back to intake."""
    attach, later = aliased(Event), aliased(Event)
    answered = exists().where(
        later.entity_id == attach.entity_id,
        later.event_type.in_(ANSWERED_EVENTS),
        later.occurred_at > attach.occurred_at,
    )
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(attach.entity_id, Task.status)
                .join(Task, Task.id == attach.entity_id)
                .where(
                    attach.event_type == "intake.attach",
                    attach.occurred_at >= _since(),
                    Task.status.in_(TERMINAL_STATUSES),
                    Task.status.notin_(CLOSED_ON_REQUEST),
                    Task.updated_at < datetime.utcnow() - timedelta(minutes=SETTLE_MINUTES),
                    ~answered,
                )
            )
        ).all()
    dropped = sorted({r.entity_id for r in rows})
    if dropped:
        return CheckResult(
            "attached_messages",
            "fail",
            f"{len(dropped)} task(s) ended without answering or resubmitting an attached message",
            {"task_ids": dropped[:20]},
        )
    return CheckResult("attached_messages", "pass", "Every attached message was answered or resubmitted", {})


async def check_slack_delivery() -> CheckResult:
    async with AsyncSessionLocal() as db:
        failed = (
            await db.execute(
                select(Event.entity_id).where(
                    Event.event_type == "slack.delivery_failed", Event.occurred_at >= _since()
                )
            )
        ).scalars().all()
    if failed:
        return CheckResult(
            "slack_delivery",
            "fail",
            f"{len(failed)} Slack delivery failure(s) in {WINDOW_HOURS}h",
            {"task_ids": sorted(set(failed))[:20]},
        )
    return CheckResult("slack_delivery", "pass", "No Slack delivery failures", {})


async def check_orphan_recoveries() -> CheckResult:
    """A run that died after Aura answered is judged again by a restarted workflow."""
    async with AsyncSessionLocal() as db:
        counts = dict(
            (
                await db.execute(
                    select(Event.event_type, func.count())
                    .where(Event.event_type.in_(ORPHAN_EVENTS), Event.occurred_at >= _since())
                    .group_by(Event.event_type)
                )
            ).all()
        )
    if counts:
        return CheckResult(
            "orphan_recoveries",
            "warn",
            f"{sum(counts.values())} run(s) died after Aura answered and were recovered in {WINDOW_HOURS}h",
            counts,
        )
    return CheckResult("orphan_recoveries", "pass", "No orphaned runs", {})


async def check_memory_hygiene() -> CheckResult:
    _, items, entries = await internal_traces()
    quoted = await canary_text_rows()
    if items or entries or quoted:
        return CheckResult(
            "memory_hygiene",
            "fail",
            f"Internal runs reached shared memory: {len(items)} row(s) from internal tasks, "
            f"{len(quoted)} row(s) quoting canary output, {len(entries)} registry entr(ies)",
            {
                "memory_ids": sorted({m.id for m in items} | {r.id for r in quoted})[:20],
                "registry_task_ids": [e.task_id for e in entries][:20],
            },
        )
    return CheckResult("memory_hygiene", "pass", "No internal traces in shared memory", {})


async def check_vector_sync() -> CheckResult:
    """Postgres and the Qdrant indexes agree, and the outbox drains."""
    if not is_vector_memory_enabled():
        return CheckResult("vector_sync", "pass", "Vector memory disabled", {})
    from app.memory.vector_sync import reconcile

    now = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        overdue = (
            await db.execute(
                select(func.count()).where(
                    VectorOutbox.done_at.is_(None),
                    VectorOutbox.next_attempt_at < now - timedelta(minutes=OUTBOX_OVERDUE_MINUTES),
                )
            )
        ).scalar_one()
        poison = (
            await db.execute(
                select(VectorOutbox.kind, VectorOutbox.ref_id, VectorOutbox.last_error).where(
                    VectorOutbox.done_at.is_(None), VectorOutbox.attempts >= OUTBOX_POISON_ATTEMPTS
                )
            )
        ).all()
    try:
        drift = await reconcile(apply=False)
    except Exception as exc:
        return CheckResult("vector_sync", "warn", f"Could not compare the indexes: {exc}", {})
    details = {**drift, "outbox_overdue": overdue, "outbox_failing": len(poison)}
    if overdue or poison:
        errors = "; ".join(f"{k}/{r}: {(e or '')[:80]}" for k, r, e in poison[:3])
        return CheckResult(
            "vector_sync",
            "fail",
            f"Vector outbox stuck: {overdue} overdue, {len(poison)} failing"
            + (f" ({errors})" if errors else ""),
            details,
        )
    unsynced = sum(
        drift.get(k, 0)
        for k in (
            "memory_missing",
            "memory_orphans",
            "registry_missing",
            "registry_orphans",
            "registry_unindexed",
            "deep_missing",
            "deep_orphans",
        )
    )
    if unsynced:
        return CheckResult(
            "vector_sync",
            "warn",
            f"{unsynced} vector(s) out of sync; the nightly reconcile repairs them",
            details,
        )
    return CheckResult("vector_sync", "pass", "Postgres and Qdrant agree; outbox drains", details)


SHIPPED = ("deployed", "rolled_back", "pr_opened")


async def _deploy_events() -> List[Event]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(Event).where(Event.event_type == "coding.deploy", Event.occurred_at >= _since())
        )).scalars())


async def check_approved_deploys() -> CheckResult:
    """Nothing ships without Kirill's own Slack approval, confirmed before the deploy."""
    shipped = [e for e in await _deploy_events() if (e.event_payload or {}).get("status") in SHIPPED]
    confirmed = {}
    if shipped:
        async with AsyncSessionLocal() as db:
            confirmed = dict((await db.execute(
                select(Event.entity_id, func.min(Event.occurred_at)).where(
                    Event.event_type == "approval.confirmed", Event.entity_id.in_({e.entity_id for e in shipped})
                ).group_by(Event.entity_id)
            )).all())
    unapproved = sorted({e.entity_id for e in shipped
                         if confirmed.get(e.entity_id) is None or confirmed[e.entity_id] > e.occurred_at})
    if unapproved:
        return CheckResult("approved_deploys", "fail", f"{len(unapproved)} coding task(s) shipped without a confirmed approval",
                           {"task_ids": unapproved[:20]})
    return CheckResult("approved_deploys", "pass", f"Every shipped coding change ({len(shipped)}) had Kirill's approval", {})


async def check_deploy_verification() -> CheckResult:
    """Every self-deploy records the exact-commit suite and the checks after the restart."""
    deployed = [e for e in await _deploy_events() if (e.event_payload or {}).get("status") == "deployed"]
    unverified = sorted({e.entity_id for e in deployed
                         if not ((e.event_payload.get("suite") or {}).get("ok") is True
                                 and e.event_payload.get("canary") in ("ok", "skipped"))})
    if unverified:
        return CheckResult("deploy_verification", "fail", f"{len(unverified)} self-deploy(s) without a verification record",
                           {"task_ids": unverified[:20]})
    return CheckResult("deploy_verification", "pass", f"Every self-deploy ({len(deployed)}) recorded its suite and checks", {})


async def check_coding_units() -> CheckResult:
    """No Claude Code unit runs without a live coding task."""
    from app.activities.coding_activities import ACTIVE_TASK_STATUSES
    from app.production.coding_readiness import live_units

    units = [u for u in await asyncio.to_thread(live_units) if u["kind"] == "claude"]
    statuses = {}
    if units:
        async with AsyncSessionLocal() as db:
            statuses = dict((await db.execute(
                select(Task.id, Task.status).where(Task.id.in_({u["task_id"] for u in units}))
            )).all())
    orphaned = [u["unit"] for u in units if statuses.get(u["task_id"]) not in ACTIVE_TASK_STATUSES]
    if orphaned:
        return CheckResult("coding_units", "fail", f"Claude Code unit(s) without a live task: {', '.join(orphaned)}",
                           {"units": orphaned})
    return CheckResult("coding_units", "pass", f"{len(units)} Claude Code unit(s), each with a live task", {})


CHECKS = (
    check_judged_deliveries,
    check_attached_messages,
    check_slack_delivery,
    check_orphan_recoveries,
    check_memory_hygiene,
    check_vector_sync,
    check_approved_deploys,
    check_deploy_verification,
    check_coding_units,
    *DEEP_MEMORY_INVARIANTS,
)


async def _run(check) -> CheckResult:
    try:
        return await check()
    except Exception as exc:
        return CheckResult(check.__name__.removeprefix("check_"), "warn", f"Check could not run: {exc}", {})


async def run_invariant_checks() -> List[CheckResult]:
    return list(await asyncio.gather(*(_run(check) for check in CHECKS)))
