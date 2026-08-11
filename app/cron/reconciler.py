"""Mirror OpenClaw cron jobs into RMP events and track scheduled work."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import OPENCLAW_HOME, is_development_mode
from app.db.models import Event
from app.metrics import inc as metrics_inc

logger = logging.getLogger("rmp.cron_reconciler")

OPENCLAW_CRON_PATH = os.path.join(OPENCLAW_HOME, "cron", "jobs.json")


def load_openclaw_cron_jobs() -> List[dict]:
    try:
        with open(OPENCLAW_CRON_PATH) as f:
            data = json.load(f)
        return data.get("jobs", [])
    except Exception as e:
        logger.warning("Could not read OpenClaw cron jobs: %s", e)
        return []


def _job_fingerprint(job: dict) -> str:
    parts = [
        job.get("id", ""),
        job.get("name", ""),
        str(job.get("enabled")),
        json.dumps(job.get("schedule"), sort_keys=True),
        json.dumps(job.get("payload"), sort_keys=True)[:500],
    ]
    return "|".join(parts)


async def reconcile_cron_once(db: AsyncSession) -> dict:
    if is_development_mode():
        return {"skipped": True, "reason": "development_mode"}

    jobs = load_openclaw_cron_jobs()
    stats = {"jobs_total": len(jobs), "enabled": 0, "events_written": 0, "jobs": []}

    for job in jobs:
        job_id = job.get("id", "unknown")
        enabled = bool(job.get("enabled"))
        if enabled:
            stats["enabled"] += 1

        fingerprint = _job_fingerprint(job)
        correlation_id = f"cron:{job_id}"

        result = await db.execute(
            select(Event)
            .where(
                Event.correlation_id == correlation_id,
                Event.event_type == "cron.job_snapshot",
            )
            .order_by(Event.occurred_at.desc())
            .limit(1)
        )
        last = result.scalar_one_or_none()
        last_fp = (last.event_payload or {}).get("fingerprint") if last else None

        if last_fp == fingerprint:
            stats["jobs"].append({"id": job_id, "name": job.get("name"), "unchanged": True})
            continue

        schedule = job.get("schedule") or {}
        payload_summary = {
            "kind": (job.get("payload") or {}).get("kind"),
            "message_preview": ((job.get("payload") or {}).get("message") or "")[:200],
        }
        event_payload: Dict[str, Any] = {
            "job_id": job_id,
            "name": job.get("name"),
            "enabled": enabled,
            "schedule_kind": schedule.get("kind"),
            "schedule_expr": schedule.get("expr"),
            "schedule_tz": schedule.get("tz"),
            "agent_id": job.get("agentId"),
            "session_target": job.get("sessionTarget"),
            "delivery_mode": (job.get("delivery") or {}).get("mode"),
            "payload_summary": payload_summary,
            "fingerprint": fingerprint,
            "last_run_status": (job.get("state") or {}).get("lastRunStatus"),
            "consecutive_errors": (job.get("state") or {}).get("consecutiveErrors", 0),
        }

        db.add(
            Event(
                correlation_id=correlation_id,
                entity_type="cron_job",
                entity_id=job_id,
                event_type="cron.job_snapshot",
                event_payload=event_payload,
            )
        )
        stats["events_written"] += 1
        stats["jobs"].append({"id": job_id, "name": job.get("name"), "snapshot": True})

        if enabled and (job.get("state") or {}).get("consecutiveErrors", 0) >= 3:
            db.add(
                Event(
                    correlation_id=correlation_id,
                    entity_type="cron_job",
                    entity_id=job_id,
                    event_type="cron.job_unhealthy",
                    event_payload={
                        "consecutive_errors": job["state"]["consecutiveErrors"],
                        "last_run_status": job["state"].get("lastRunStatus"),
                    },
                )
            )
            stats["events_written"] += 1

    await db.commit()
    if stats["events_written"] > 0 or stats["jobs_total"] > 0:
        metrics_inc("cron_reconcile")
    return stats


async def cron_reconciler_loop(stop_event) -> None:
    import asyncio

    from app.db.database import AsyncSessionLocal

    interval = 120
    logger.info("Cron reconciler started (interval=%ss)", interval)
    while not stop_event.is_set():
        try:
            async with AsyncSessionLocal() as db:
                result = await reconcile_cron_once(db)
                if not result.get("skipped"):
                    logger.debug("Cron reconcile: %s", result)
        except Exception:
            logger.exception("Cron reconciler error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass
    logger.info("Cron reconciler stopped")
