"""Canary failure detection, deterministic remediation, and ops Slack alerts."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import RMP_ROOT as _RMP_ROOT
from app.production.alerting import send_alert
from app.production.ops_notify import notify_ops_slack
from app.llm.quota_broker import reap_stale_llm_slots_sync

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
    "temporal-dev",
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
    if data.get("status") != "completed":
        return CanaryIssue(
            "health_canary",
            data.get("status", "failed"),
            f"Hourly health canary status={data.get('status')}",
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
    if status != "completed":
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
    if age > timedelta(hours=MEMORY_MAX_AGE_HOURS):
        return CanaryIssue(
            "memory_canary",
            "stale",
            f"Last memory canary {age.total_seconds() / 3600:.1f}h old",
            data,
        )
    return None


def evaluate_canaries() -> List[CanaryIssue]:
    issues: List[CanaryIssue] = []
    for fn in (evaluate_health_canary, evaluate_memory_canary):
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


def attempt_remediation(issues: List[CanaryIssue]) -> List[str]:
    """Deterministic fixes only — release leaked LLM slots, restart inactive services."""
    actions: List[str] = []
    reaped = reap_stale_llm_slots_sync()
    if reaped:
        actions.extend([f"reaped slot {r}" for r in reaped])
    restarted: set[str] = set()
    for issue in issues:
        for unit in SERVICE_UNITS:
            if unit in restarted:
                continue
            if not _systemctl_is_active(unit):
                if _restart_unit(unit):
                    actions.append(f"restarted {unit}")
                    restarted.add(unit)
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
) -> None:
    HEALTH_CANARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "task_id": task_id,
        "error": error,
        "finished_at": datetime.utcnow().isoformat() + "Z",
    }
    HEALTH_CANARY_PATH.write_text(json.dumps(payload, indent=2))


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
    if result["remediation"]:
        await asyncio.sleep(5)
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
        lines.append("Issue persists — manual check recommended.")
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
