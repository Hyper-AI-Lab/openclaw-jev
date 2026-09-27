"""Canary failure detection, deterministic remediation, and ops Slack alerts."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import RMP_ROOT as _RMP_ROOT
from app.production.alerting import send_alert
from app.production.ops_notify import notify_ops_slack
from app.llm.quota_broker import reap_stale_llm_slots_sync, seconds_until_any_key_ready

logger = logging.getLogger("rmp.canary_sentinel")

RMP_ROOT = Path(_RMP_ROOT)
HEALTH_CANARY_PATH = RMP_ROOT / "data" / "last_health_canary.json"
MEMORY_CANARY_PATH = RMP_ROOT / "data" / "last_memory_canary.json"
ALERT_STATE_PATH = RMP_ROOT / "data" / "last_canary_alert.json"

HEALTH_MAX_AGE_HOURS = 2
MEMORY_MAX_AGE_HOURS = 25
ALERT_COOLDOWN_HOURS = 4

SERVICE_UNITS = (
    "rmp-api",
    "rmp-worker",
    "openclaw-gateway",
    "temporal",
    "rmp-qdrant",
)


@dataclass
class CanaryIssue:
    name: str
    status: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


def _read_json(path: Path) -> Optional[dict]:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def evaluate_health_canary() -> Optional[CanaryIssue]:
    data = _read_json(HEALTH_CANARY_PATH)
    if not data:
        return CanaryIssue(
            "health_canary",
            "missing",
            "No hourly health canary result on disk (canary may not have completed since last deploy)",
        )
    finished = _parse_ts(data.get("finished_at"))
    if not finished:
        return CanaryIssue("health_canary", "invalid", "Health canary missing finished_at", data)
    age = datetime.utcnow() - finished
    status = data.get("status") or "failed"
    # Deferred = we skipped competing with user work. Recent defer is healthy.
    if status == "deferred":
        if age > timedelta(hours=HEALTH_MAX_AGE_HOURS):
            return CanaryIssue(
                "health_canary",
                "stale",
                f"Health canary deferred {age.total_seconds() / 3600:.1f}h (never recovered)",
                data,
            )
        return None
    if status != "completed":
        return CanaryIssue(
            "health_canary",
            status,
            f"Hourly health canary status={status}",
            data,
        )
    if age > timedelta(hours=HEALTH_MAX_AGE_HOURS):
        return CanaryIssue(
            "health_canary",
            "stale",
            f"Last health canary {age.total_seconds() / 3600:.1f}h old",
            data,
        )
    return None


def inspect_memory_canary_transcript(task_id: str) -> Dict[str, Any]:
    """Read rmp_task transcript via SQLite/jsonl. Three facts: dispatch vs prompt vs memory."""
    from app.openclaw_sessions import get_session_entry, read_transcript_lines

    key = f"agent:main:rmp_task_{task_id}"
    entry = get_session_entry(key)
    sid = str(entry.get("sessionId") or entry.get("id") or "")
    lines = read_transcript_lines(sid) if sid else []
    blob = "\n".join(lines)
    low = blob.lower()
    has = bool(blob.strip())
    memory_ok = "process-scoped memory" in low
    prompt_ok = (
        "do not use memory_search" in low or "do not use `memory_search`" in low
    )
    search_bad = ("memory_search" in low) and not prompt_ok
    jsonl = ""
    if sid:
        from app.openclaw_sessions import SESSIONS_DIR

        candidate = SESSIONS_DIR / f"{sid}.jsonl"
        if candidate.is_file():
            jsonl = str(candidate)
    return {
        "session_file": jsonl,
        "session_id": sid,
        "has_transcript": has,
        "memory_ok": int(memory_ok),
        "prompt_ok": int(prompt_ok),
        "search_bad": int(search_bad),
    }


def evaluate_memory_canary() -> Optional[CanaryIssue]:
    data = _read_json(MEMORY_CANARY_PATH)
    if not data:
        return CanaryIssue(
            "memory_canary",
            "missing",
            "No memory canary result on disk",
        )
    finished = _parse_ts(data.get("finished_at"))
    if not finished:
        return CanaryIssue("memory_canary", "invalid", "Memory canary missing finished_at", data)
    age = datetime.utcnow() - finished
    status = data.get("status", "")
    if status in ("running", "created"):
        status = "timeout"
    if status == "deferred":
        if age > timedelta(hours=MEMORY_MAX_AGE_HOURS):
            return CanaryIssue(
                "memory_canary",
                "stale",
                f"Memory canary deferred {age.total_seconds() / 3600:.1f}h",
                data,
            )
        return None
    if status == "inconclusive":
        return CanaryIssue(
            "memory_canary",
            "inconclusive",
            "Memory canary transcript missing (inconclusive)",
            data,
        )
    # Transcript checks are the memory canary's purpose. A Temporal timeout
    # after memory_ok/prompt_ok is a dispatch-completion issue (health canary).
    transcript_ok = bool(data.get("memory_ok")) and bool(data.get("prompt_ok")) and not data.get(
        "search_bad"
    )
    if status != "completed":
        if transcript_ok:
            return None
        return CanaryIssue(
            "memory_canary",
            status or "failed",
            f"Memory canary status={status}",
            data,
        )
    if data.get("search_bad"):
        return CanaryIssue(
            "memory_canary",
            "memory_path",
            "Memory canary detected workspace memory_search dominance",
            data,
        )
    if not transcript_ok:
        return CanaryIssue(
            "memory_canary",
            "memory_unproven",
            "Memory canary completed but memory_ok/prompt_ok not proven",
            data,
        )
    if age > timedelta(hours=MEMORY_MAX_AGE_HOURS):
        return CanaryIssue(
            "memory_canary",
            "stale",
            f"Last memory canary {age.total_seconds() / 3600:.1f}h old",
            data,
        )
    return None


def evaluate_runtime_code_sync() -> Optional[CanaryIssue]:
    from app.production.runtime_sync import runtime_sync_status

    status = runtime_sync_status()
    overall = status.get("status")
    if overall == "ok":
        return None
    if overall == "missing":
        return CanaryIssue(
            "runtime_code_sync",
            "missing",
            "Runtime boot stamps missing (API/worker not stamped after start)",
            status,
        )
    stale = ", ".join(status.get("stale_services") or []) or "rmp-api/rmp-worker"
    return CanaryIssue(
        "runtime_code_sync",
        "stale",
        f"On-disk code newer than running process ({stale})",
        status,
    )


def evaluate_canaries() -> List[CanaryIssue]:
    issues: List[CanaryIssue] = []
    for fn in (evaluate_health_canary, evaluate_memory_canary, evaluate_runtime_code_sync):
        issue = fn()
        if issue:
            issues.append(issue)
    return issues


def _systemctl_is_active(unit: str) -> bool:
    try:
        subprocess.run(
            ["systemctl", "is-active", "--quiet", f"{unit}.service"],
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _restart_unit(unit: str) -> bool:
    try:
        subprocess.run(
            ["systemctl", "restart", f"{unit}.service"],
            check=True,
            capture_output=True,
            timeout=120,
        )
        return True
    except Exception as exc:
        logger.warning("Restart failed for %s: %s", unit, exc)
        return False


RESTART_UNITS_ON_STALE = ("rmp-api", "rmp-worker")
REMEDIATION_COOLDOWN_MINUTES = 20
REMEDIATION_STATE_PATH = RMP_ROOT / "data" / "last_canary_remediation.json"

# Soft health_canary failures that often mean "busy / LLM starved", not dead runtime.
SOFT_HEALTH_STATUSES = frozenset({"timeout", "failed"})
SOFT_MEMORY_STATUSES = frozenset({"timeout", "failed", "running", "created"})
CANARY_SCRIPT = RMP_ROOT / "ops" / "canary.sh"
RECOVERY_MAX_POLLS = 24  # 4 minutes
LLM_COOLDOWN_WAIT_CAP_SEC = 60.0


def _remediation_cooldown_active(key: str = "restart_runtime") -> bool:
    state = _read_json(REMEDIATION_STATE_PATH) or {}
    ts = _parse_ts(state.get(key))
    if not ts:
        return False
    return datetime.utcnow() - ts < timedelta(minutes=REMEDIATION_COOLDOWN_MINUTES)


def _record_remediation(key: str = "restart_runtime") -> None:
    state = _read_json(REMEDIATION_STATE_PATH) or {}
    state[key] = datetime.utcnow().isoformat() + "Z"
    REMEDIATION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REMEDIATION_STATE_PATH.write_text(json.dumps(state, indent=2))


def count_active_user_tasks_sync() -> int:
    """Count non-terminal user tasks that would be hurt by a worker restart."""
    try:
        from sqlalchemy import create_engine, text

        from app.db.database import DATABASE_URL
        from app.notification_policy import is_internal_task

        sync_url = DATABASE_URL.replace("+asyncpg", "+psycopg2")
        engine = create_engine(sync_url)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, task_type, goal FROM tasks "
                    "WHERE status IN ('running', 'created', 'pending_user_input')"
                )
            ).fetchall()
        count = 0
        for _id, task_type, goal in rows:
            if is_internal_task(goal or "", task_type or "", []):
                continue
            count += 1
        return count
    except Exception as exc:
        logger.warning("count_active_user_tasks_sync failed: %s", exc)
        return 0


def cancel_task_sync(task_id: str, reason: str = "canary_timeout") -> bool:
    if not task_id:
        return False
    try:
        from sqlalchemy import create_engine, text

        from app.db.database import DATABASE_URL

        sync_url = DATABASE_URL.replace("+asyncpg", "+psycopg2")
        engine = create_engine(sync_url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE tasks SET status = 'cancelled' "
                    "WHERE id = :id AND status IN ('running', 'created', 'pending_user_input')"
                ),
                {"id": task_id},
            )
        try:

            async def _term() -> None:
                from temporalio.client import Client

                client = await Client.connect("localhost:7233")
                for wid in (f"workflow-{task_id}", f"{task_id}-plan-deliver-1"):
                    try:
                        await client.get_workflow_handle(wid).terminate(reason)
                    except Exception:
                        pass

            asyncio.run(_term())
        except Exception as exc:
            logger.debug("Temporal terminate after canary cancel skipped: %s", exc)
        return True
    except Exception as exc:
        logger.warning("cancel_task_sync %s failed: %s", task_id[:8], exc)
        return False


def list_stuck_canary_task_ids_sync() -> List[str]:
    """Non-terminal canary/system probe tasks that can pin LLM slots."""
    try:
        from sqlalchemy import create_engine, text

        from app.db.database import DATABASE_URL

        sync_url = DATABASE_URL.replace("+asyncpg", "+psycopg2")
        engine = create_engine(sync_url)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, task_type, goal FROM tasks "
                    "WHERE status IN ('running', 'created', 'pending_user_input')"
                )
            ).fetchall()
        ids: List[str] = []
        for task_id, task_type, goal in rows:
            t = (task_type or "").lower()
            g = (goal or "").upper()
            if t == "canary" or "RMP CANARY" in g or "MEMORY CANARY" in g:
                ids.append(str(task_id))
        return ids
    except Exception as exc:
        logger.warning("list_stuck_canary_task_ids_sync failed: %s", exc)
        return []


def cancel_stuck_canary_tasks_sync(reason: str = "canary_timeout") -> List[str]:
    cancelled: List[str] = []
    for tid in list_stuck_canary_task_ids_sync():
        if cancel_task_sync(tid, reason):
            cancelled.append(tid)
    return cancelled


def rerun_health_canary_sync(*, max_polls: int = RECOVERY_MAX_POLLS) -> bool:
    """Re-prove the dispatch path. Must not recurse into the sentinel."""
    env = os.environ.copy()
    env["RMP_CANARY_SKIP_SENTINEL"] = "1"
    env["RMP_CANARY_MAX_POLLS"] = str(max_polls)
    timeout_sec = max_polls * 10 + 90
    try:
        proc = subprocess.run(
            ["bash", str(CANARY_SCRIPT)],
            cwd=str(RMP_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except Exception as exc:
        logger.warning("recovery health canary failed to start: %s", exc)
        return False
    if proc.returncode != 0:
        logger.warning(
            "recovery health canary rc=%s tail=%s",
            proc.returncode,
            (proc.stdout or proc.stderr or "")[-400:],
        )
        return False
    return True


def _wait_for_llm_capacity() -> Optional[str]:
    try:
        wait_sec = float(seconds_until_any_key_ready() or 0.0)
    except Exception:
        wait_sec = 0.0
    if wait_sec <= 1:
        return None
    sleep_for = min(wait_sec, LLM_COOLDOWN_WAIT_CAP_SEC)
    time.sleep(sleep_for)
    return f"waited {sleep_for:.0f}s for LLM key cooldown"


def attempt_remediation(issues: List[CanaryIssue]) -> List[str]:
    """Deterministic fixes — reap LLM slots, cancel stuck canaries, restart down/stale units.

    Soft health_canary timeout/failed does NOT restart rmp-api/worker. That is LLM
    starvation or a stuck probe, not a dead runtime. Recovery is cancel + reap +
    re-run (see run_sentinel). Restart only for down units or stale/missing code.
    """
    actions: List[str] = []
    reaped = reap_stale_llm_slots_sync()
    if reaped:
        actions.extend([f"reaped slot {r}" for r in reaped])
    restarted: set[str] = set()
    for unit in SERVICE_UNITS:
        if unit in restarted:
            continue
        if not _systemctl_is_active(unit):
            if _restart_unit(unit):
                actions.append(f"restarted {unit}")
                restarted.add(unit)

    extra_ids: List[str] = []
    for issue in issues:
        if issue.name in {"health_canary", "memory_canary"} and issue.status in (
            SOFT_HEALTH_STATUSES | SOFT_MEMORY_STATUSES
        ):
            tid = str((issue.details or {}).get("task_id") or "")
            if tid and cancel_task_sync(tid, f"{issue.name}_{issue.status}"):
                extra_ids.append(tid)
                actions.append(f"cancelled canary task {tid[:8]}")

    stuck = cancel_stuck_canary_tasks_sync("canary_sentinel_recovery")
    for tid in stuck:
        if tid not in extra_ids:
            actions.append(f"cancelled canary task {tid[:8]}")

    reaped2 = reap_stale_llm_slots_sync()
    if reaped2:
        actions.extend([f"reaped slot {r}" for r in reaped2])

    hard_reload = any(
        i.name == "runtime_code_sync" and i.status in {"stale", "missing"} for i in issues
    )
    hard_health = any(
        i.name == "health_canary" and i.status in {"stale", "missing"} for i in issues
    )
    active_users = count_active_user_tasks_sync()
    needs_runtime_reload = hard_reload or hard_health
    if needs_runtime_reload and active_users > 0:
        actions.append(
            f"deferred runtime restart ({active_users} active user task(s); "
            "stale/missing canary only)"
        )
        needs_runtime_reload = False

    if needs_runtime_reload and not _remediation_cooldown_active():
        for unit in RESTART_UNITS_ON_STALE:
            if unit in restarted:
                continue
            if _restart_unit(unit):
                actions.append(f"restarted {unit} (stale-runtime/canary)")
                restarted.add(unit)
        if any(u in restarted for u in RESTART_UNITS_ON_STALE):
            _record_remediation()
    return actions


def _alert_cooldown_active(incident_key: str) -> bool:
    state = _read_json(ALERT_STATE_PATH) or {}
    last = state.get(incident_key)
    ts = _parse_ts(last)
    if not ts:
        return False
    return datetime.utcnow() - ts < timedelta(hours=ALERT_COOLDOWN_HOURS)


def _record_alert(incident_key: str) -> None:
    state = _read_json(ALERT_STATE_PATH) or {}
    state[incident_key] = datetime.utcnow().isoformat() + "Z"
    ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERT_STATE_PATH.write_text(json.dumps(state, indent=2))


def write_health_canary_result(
    *,
    status: str,
    task_id: str = "",
    error: str = "",
    **extra: Any,
) -> None:
    HEALTH_CANARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "task_id": task_id,
        "error": error,
        "finished_at": datetime.utcnow().isoformat() + "Z",
        **extra,
    }
    HEALTH_CANARY_PATH.write_text(json.dumps(payload, indent=2))


def maybe_mark_health_canary_deferred(*, reason: str, active_users: int = 0) -> bool:
    """If last health result is a failure, mark deferred instead of leaving timeout on disk."""
    data = _read_json(HEALTH_CANARY_PATH) or {}
    if data.get("status") == "completed":
        return False
    write_health_canary_result(
        status="deferred",
        task_id=str(data.get("task_id") or ""),
        error=reason,
        prior_status=data.get("status"),
        active_users=active_users,
    )
    return True


def maybe_mark_memory_canary_deferred(*, reason: str, active_users: int = 0) -> bool:
    data = _read_json(MEMORY_CANARY_PATH) or {}
    if data.get("status") == "completed":
        return False
    if data.get("memory_ok") and data.get("prompt_ok") and not data.get("search_bad"):
        return False
    MEMORY_CANARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **data,
        "status": "deferred",
        "error": reason,
        "prior_status": data.get("status"),
        "active_users": active_users,
        "finished_at": datetime.utcnow().isoformat() + "Z",
    }
    MEMORY_CANARY_PATH.write_text(json.dumps(payload, indent=2))
    return True


async def handle_canary_failure(
    source: str,
    *,
    status: str,
    task_id: str = "",
    error: str = "",
) -> Dict[str, Any]:
    """Called when a canary script fails — persist result and run sentinel."""
    if source == "health_canary":
        write_health_canary_result(status=status, task_id=task_id, error=error)
    return await run_sentinel(trigger=source)


async def run_sentinel(*, trigger: str = "scheduled") -> Dict[str, Any]:
    issues = evaluate_canaries()
    result: Dict[str, Any] = {
        "trigger": trigger,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "issues": [{"name": i.name, "status": i.status, "message": i.message} for i in issues],
        "remediation": [],
        "alerted": False,
    }

    if not issues:
        return result

    result["remediation"] = attempt_remediation(issues)

    wait_note = _wait_for_llm_capacity()
    if wait_note:
        result["remediation"].append(wait_note)

    remaining = evaluate_canaries()
    needs_prove = any(
        (i.name == "health_canary" and i.status in SOFT_HEALTH_STATUSES)
        or (i.name == "memory_canary" and i.status in SOFT_MEMORY_STATUSES)
        for i in remaining
    )
    if needs_prove:
        active_users = count_active_user_tasks_sync()
        if active_users > 0:
            maybe_mark_health_canary_deferred(
                reason="active_user_tasks", active_users=active_users
            )
            result["remediation"].append(
                f"deferred recovery re-run ({active_users} active user task(s))"
            )
        else:
            ok = rerun_health_canary_sync()
            result["recovery_canary_ok"] = ok
            result["remediation"].append(
                "recovery health canary ok" if ok else "recovery health canary failed"
            )

    issues = evaluate_canaries()
    result["issues_after_remediation"] = [
        {"name": i.name, "status": i.status, "message": i.message} for i in issues
    ]

    if not issues:
        result["resolved_by_remediation"] = True
        return result

    lines = [
        "⚠️ RMP canary failure detected",
        f"Trigger: {trigger}",
    ]
    for issue in issues:
        lines.append(f"• {issue.name}: {issue.message}")
    if result["remediation"]:
        lines.append(f"Auto-fix attempted: {', '.join(result['remediation'])}")
        lines.append("Recovery re-run did not clear the incident.")
    else:
        lines.append("No automatic fix applied.")

    incident_key = ":".join(sorted({i.name for i in issues}))
    if not _alert_cooldown_active(incident_key):
        message = "\n".join(lines)
        alerted = await notify_ops_slack(message, incident_id=incident_key)
        await send_alert(
            "canary.failure",
            message.replace("\n", " ")[:500],
            severity="error",
            context={"issues": result["issues"], "trigger": trigger},
        )
        if alerted:
            _record_alert(incident_key)
        result["alerted"] = alerted
    else:
        result["alerted"] = False
        result["alert_suppressed"] = "cooldown"

    return result


async def _main_async(args: List[str]) -> int:
    trigger = "scheduled"
    if "--trigger" in args:
        trigger = args[args.index("--trigger") + 1]
    result = await run_sentinel(trigger=trigger)
    print(json.dumps(result, indent=2))
    return 0 if not result.get("issues") else 1


def main() -> None:
    import sys

    raise SystemExit(asyncio.run(_main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
