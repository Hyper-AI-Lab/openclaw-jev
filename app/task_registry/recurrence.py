"""Recurrence keys and fast-path policies for cron/canary/heartbeat."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import select

from app.config import get_task_registry_config


def derive_recurrence_key(
    session_key: str,
    intent: str,
    tags: Optional[List[str]] = None,
) -> Optional[str]:
    tags = tags or []
    tag_set = {t.lower() for t in tags}
    if "canary" in tag_set or "RMP CANARY" in (intent or "").upper():
        if "memory-canary" in tag_set:
            return "recurrence:memory_canary"
        return "recurrence:health_canary"
    if "heartbeat" in tag_set or (
        "heartbeat" in (intent or "").lower() and "HEARTBEAT.md" in (intent or "")
    ):
        return "recurrence:heartbeat"
    if "cron" in tag_set or (intent or "").startswith("[cron:"):
        cron_match = re.search(r"\[cron:([^\]]+)\]", intent or "")
        label = cron_match.group(1).strip() if cron_match else ""
        base = label or session_key or "cron"
        digest = hashlib.sha256(base.encode()).hexdigest()[:16]
        return f"recurrence:cron:{digest}"
    return None


def _interval_minutes(recurrence_key: Optional[str]) -> int:
    cfg = get_task_registry_config()
    intervals = cfg.get("recurrence_intervals") or {}
    if not recurrence_key:
        return int(intervals.get("cron_default", 55))
    if recurrence_key == "recurrence:heartbeat":
        return int(intervals.get("heartbeat", 25))
    if recurrence_key == "recurrence:health_canary":
        return int(intervals.get("health_canary", 55))
    if recurrence_key == "recurrence:memory_canary":
        return int(intervals.get("memory_canary", 360))
    if recurrence_key.startswith("recurrence:cron:"):
        return int(intervals.get("cron_default", 55))
    return int(intervals.get("cron_default", 55))


async def skip_valid_decision(
    recurrence_key: Optional[str],
) -> Optional[Tuple[str, str, Optional[str]]]:
    """If last completed recurrent run is within interval, skip."""
    if not recurrence_key:
        return None
    from app.db.database import AsyncSessionLocal
    from app.db.models import TaskRegistryEntry

    cutoff_interval = _interval_minutes(recurrence_key)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TaskRegistryEntry)
            .where(TaskRegistryEntry.recurrence_key == recurrence_key)
            .order_by(TaskRegistryEntry.task_ended_at.desc().nullslast())
            .limit(1)
        )
        entry = result.scalar_one_or_none()
    if not entry or not entry.task_ended_at:
        return None
    age = datetime.utcnow() - entry.task_ended_at
    if age <= timedelta(minutes=cutoff_interval):
        return (
            "skip_valid",
            f"Last {recurrence_key} completed {int(age.total_seconds() // 60)}m ago "
            f"(interval {cutoff_interval}m)",
            entry.task_id,
        )
    return None


def _is_noop_outcome(text: str) -> bool:
    from app.notification_policy import is_silent_system_ack

    outcome = (text or "").strip()
    if not outcome:
        return False
    if is_silent_system_ack(outcome):
        return True
    cfg = get_task_registry_config()
    phrases = cfg.get("noop_phrases") or [
        "nothing new",
        "no actionable",
        "stay quiet",
        "no updates",
        "nothing to report",
        "nothing actionable",
    ]
    lower = outcome.lower()
    return any(phrase in lower for phrase in phrases)


async def skip_noop_decision(
    recurrence_key: Optional[str],
) -> Optional[Tuple[str, str, Optional[str]]]:
    """If last recurrent run had a silent/no-op outcome within interval, skip."""
    if not recurrence_key:
        return None
    from app.db.database import AsyncSessionLocal
    from app.db.models import TaskRegistryEntry

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TaskRegistryEntry)
            .where(TaskRegistryEntry.recurrence_key == recurrence_key)
            .order_by(TaskRegistryEntry.task_ended_at.desc().nullslast())
            .limit(1)
        )
        entry = result.scalar_one_or_none()
    if not entry or not entry.task_ended_at:
        return None
    if not _is_noop_outcome(entry.outcome_summary or ""):
        return None
    age = datetime.utcnow() - entry.task_ended_at
    interval = _interval_minutes(recurrence_key)
    if age > timedelta(minutes=interval):
        return None
    return (
        "skip_noop",
        f"Last {recurrence_key} had no actionable outcome "
        f"({int(age.total_seconds() // 60)}m ago, interval {interval}m)",
        entry.task_id,
    )


async def supersede_decision(
    recurrence_key: Optional[str],
) -> Optional[Tuple[str, str, Optional[str]]]:
    """If last recurrent run failed and is outside retry interval, supersede."""
    if not recurrence_key:
        return None
    from app.db.database import AsyncSessionLocal
    from app.db.models import TaskRegistryEntry

    failed_statuses = frozenset({"failed", "failed_terminal", "compensated"})
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TaskRegistryEntry)
            .where(TaskRegistryEntry.recurrence_key == recurrence_key)
            .order_by(TaskRegistryEntry.task_ended_at.desc().nullslast())
            .limit(1)
        )
        entry = result.scalar_one_or_none()
    if not entry or not entry.task_ended_at:
        return None
    if (entry.terminal_status or "") not in failed_statuses:
        return None
    age = datetime.utcnow() - entry.task_ended_at
    interval = _interval_minutes(recurrence_key)
    if age <= timedelta(minutes=interval):
        return None
    return (
        "supersede",
        f"Prior {recurrence_key} failed ({entry.terminal_status}); "
        f"replacing stale instance ({int(age.total_seconds() // 60)}m ago)",
        entry.task_id,
    )


def derive_task_kind(
    recurrence_key: Optional[str],
    tags: Optional[List[str]] = None,
    *,
    task_kind_hint: Optional[str] = None,
) -> str:
    if task_kind_hint == "durable":
        return "durable"
    tags = tags or []
    tag_set = {t.lower() for t in tags}
    if "durable-task" in tag_set or "durable" in tag_set:
        return "durable"
    if recurrence_key:
        return "recurrent"
    return "one_shot"


def should_bypass_intake_llm(tags: Optional[List[str]], intent: str) -> bool:
    """Health canary always create_fresh — skip LLM adjudication."""
    tag_set = {t.lower() for t in (tags or [])}
    if "canary" in tag_set and "memory-canary" not in tag_set:
        return True
    if "RMP CANARY" in (intent or "").upper() and "memory-canary" not in tag_set:
        return True
    return False


def _normalize_intent(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def fast_path_decision(
    *,
    active_tasks: list,
    recurrence_key: Optional[str],
    tags: Optional[List[str]] = None,
    intent: str = "",
    session_key: str = "",
) -> Optional[Tuple[str, str, Optional[str]]]:
    """Return (decision, rationale, target_task_id) or None to continue to LLM/gate."""
    tag_set = {t.lower() for t in (tags or [])}
    if "force-canary-run" in tag_set:
        return (
            "create_fresh",
            "Manual memory canary force-run (skip_valid bypass)",
            None,
        )
    if should_bypass_intake_llm(tags, intent):
        return (
            "create_fresh",
            "Health canary always runs (LLM bypass)",
            None,
        )
    if recurrence_key and active_tasks:
        for t in active_tasks:
            if t.get("recurrence_key") == recurrence_key:
                return (
                    "wait_active",
                    f"Recurrent job already active ({recurrence_key})",
                    t.get("task_id"),
                )
    intent_norm = _normalize_intent(intent)
    if intent_norm and active_tasks:
        for t in active_tasks:
            t_session = t.get("session_key") or ""
            same_session = not session_key or not t_session or t_session == session_key
            if not same_session:
                continue
            goal = (t.get("goal") or t.get("goal_snippet") or "").strip()
            if goal and _normalize_intent(goal) == intent_norm:
                return (
                    "attach_active",
                    "Duplicate intent matches active task in session",
                    t.get("task_id"),
                )
    return None
