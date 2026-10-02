"""Readiness of coding jobs: Claude Code itself, the isolation around it, and the jobs in systemd and on disk.

Also the views behind ``GET /api/coding/status`` and ``GET /api/coding/jobs/{task_id}``.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.coding import firewall, runner
from app.coding.credentials import read_meta
from app.coding.units import CLAUDE_BIN, JOBS_DIR, MANAGED_SETTINGS, RUNS_DIR, TOKEN_ENV_FILE, TOKEN_META_FILE
from app.config import RMP_DATA_DIR, RMP_ROOT, get_coding_config
from app.production.readiness import CheckResult

SMOKE_RECORD = Path(RMP_DATA_DIR) / "coding" / "claude_smoke.json"
MANAGED_SETTINGS_SOURCE = Path(RMP_ROOT) / "ops" / "aura_coder" / "managed-settings.json"
TOKEN_WARN_DAYS = 30
STUCK_MARGIN_SEC = 900
UNIT = re.compile(r"^aura-(claude|verify|collect|deploy)-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def _sha(path: Path) -> Optional[str]:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def token_days_left() -> Optional[int]:
    meta = read_meta(TOKEN_META_FILE) or {}
    if not meta.get("expires_at"):
        return None
    return (datetime.fromisoformat(meta["expires_at"]) - _now()).days


def installed_version() -> Optional[str]:
    """The pinned install's version, from the versions/<version> path the binary links to; never executed as root."""
    return CLAUDE_BIN.resolve().name if CLAUDE_BIN.exists() else None


def check_claude_code() -> CheckResult:
    pinned = get_coding_config()["claude_version"]
    version, days = installed_version(), token_days_left()
    smoke = json.loads(SMOKE_RECORD.read_text()) if SMOKE_RECORD.is_file() else None
    problems, warnings = [], []
    if version is None:
        problems.append("the claude binary is missing")
    elif version != pinned:
        problems.append(f"claude is {version}, pinned {pinned}")
    if not TOKEN_ENV_FILE.is_file() or TOKEN_ENV_FILE.stat().st_size == 0 or days is None:
        problems.append("no Claude Code token (run ops/claude_code_login.sh)")
    elif days < 0:
        problems.append("the Claude Code token has expired (run ops/claude_code_login.sh)")
    elif days < TOKEN_WARN_DAYS:
        warnings.append(f"the Claude Code token expires in {days} days")
    if smoke is None:
        warnings.append("no smoke run recorded (ops/claude_code_smoke.py)")
    elif not smoke.get("ok"):
        warnings.append("the last smoke run failed")
    details = {"version": version, "pinned": pinned, "token_days_left": days,
               "smoke_at": (smoke or {}).get("at"), "smoke_ok": (smoke or {}).get("ok")}
    status = "fail" if problems else "warn" if warnings else "pass"
    message = "; ".join(problems + warnings) or (
        f"Claude Code {version}; token valid for {days} more days; last smoke run passed ({details['smoke_at']})")
    return CheckResult("claude_code", status, message, details)


def check_coding_isolation() -> CheckResult:
    problems = []
    if not firewall.active():
        problems.append("the aura_coder firewall is not loaded")
    uncovered = firewall.uncovered_listeners(get_coding_config()["blocked_tcp_ports"])
    if uncovered:
        problems.append(f"loopback listeners open to aura-coder: {', '.join(uncovered)}")
    if _sha(MANAGED_SETTINGS) is None or _sha(MANAGED_SETTINGS) != _sha(MANAGED_SETTINGS_SOURCE):
        problems.append(f"{MANAGED_SETTINGS} is missing or differs from the repo's copy")
    if problems:
        return CheckResult("coding_isolation", "fail", "; ".join(problems), {"uncovered": uncovered})
    return CheckResult("coding_isolation", "pass",
                       "Firewall loaded, every loopback listener covered, managed settings intact", {})


def live_units() -> List[Dict[str, Any]]:
    """Active coding units, with the task each belongs to and when it started."""
    listed = subprocess.run(["systemctl", "list-units", "--plain", "--no-legend", "--state=active,activating", "aura-*"],
                            capture_output=True, text=True, timeout=30)
    units = []
    for line in listed.stdout.splitlines():
        name = line.split()[0] if line.strip() else ""
        match = UNIT.match(name)
        if not match:
            continue
        shown = subprocess.run(["systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "--value", name],
                               capture_output=True, text=True, timeout=30).stdout.strip()
        units.append({"unit": name.removesuffix(".service"), "kind": match.group(1), "task_id": match.group(2),
                      "active_usec": int(shown) if shown.isdigit() else None})
    return units


def _uptime_usec() -> int:
    return int(float(Path("/proc/uptime").read_text().split()[0]) * 1_000_000)


def check_coding_jobs() -> CheckResult:
    cfg = get_coding_config()
    limit_usec = (int(cfg["run_timeout_sec"]) + STUCK_MARGIN_SEC) * 1_000_000
    now_usec = _uptime_usec()
    stuck = [u["unit"] for u in live_units()
             if u["kind"] == "claude" and u["active_usec"] and now_usec - u["active_usec"] > limit_usec]
    orphans = sorted(p.name for p in JOBS_DIR.iterdir()
                     if p.is_dir() and not (RUNS_DIR / p.name.removesuffix("-deploy") / "job.json").is_file()) \
        if JOBS_DIR.is_dir() else []
    problems = ([f"Claude Code units running past their limit: {', '.join(stuck)}"] if stuck else []) + \
               ([f"job checkouts without a job record: {', '.join(orphans[:10])}"] if orphans else [])
    if problems:
        return CheckResult("coding_jobs", "warn", "; ".join(problems), {"stuck": stuck, "orphans": orphans})
    return CheckResult("coding_jobs", "pass", "No stuck coding units or orphan checkouts", {})


CODING_CHECKS = (check_claude_code, check_coding_isolation, check_coding_jobs)


def run_coding_checks() -> List[CheckResult]:
    results = []
    for check in CODING_CHECKS:
        try:
            results.append(check())
        except Exception as exc:
            results.append(CheckResult(check.__name__.removeprefix("check_"), "warn", f"Check could not run: {exc}", {}))
    return results


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def coding_status() -> Dict[str, Any]:
    from app.activities.coding_activities import slot_holder

    cfg = get_coding_config()
    return {"enabled": bool(cfg.get("enabled")), "claude_version": installed_version(), "pinned": cfg["claude_version"],
            "token_days_left": token_days_left(), "slot_holder": slot_holder(), "live_units": live_units(),
            "repositories": {name: {"remote": e["remote"], "deploy": e.get("deploy")} for name, e in cfg["repositories"].items()},
            "jobs": sorted((p.parent.name for p in RUNS_DIR.glob("*/job.json")), reverse=True)[:50]}


def coding_job(task_id: str) -> Optional[Dict[str, Any]]:
    """What RMP recorded for one coding task: the job, each Claude Code run, each verification, the deploy."""
    root = RUNS_DIR / task_id
    job = _read_json(root / "job.json")
    if job is None:
        return None
    runs = []
    for path in sorted((p for p in root.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name)):
        run = runner.Run(task_id, int(path.name), RUNS_DIR)
        result = runner.finish(run)
        runs.append({"number": run.number, "unit": run.unit, "active": runner.unit_active(run.unit),
                     "meta": _read_json(path / "meta.json"), "exit": result.exit, "outcome": result.kind,
                     "num_turns": result.num_turns, "usage": result.usage, "error": result.error})
    verifications = [{"attempt": p.name.removeprefix("verify-"), **(_read_json(p / "result.json") or {})}
                     for p in sorted(root.glob("verify-*"))]
    for item in verifications:
        for command in item.get("commands") or []:
            command.pop("tail", None)
    spec = _read_json(root / "deploy.json") or {}
    return {"job": job, "runs": runs, "verifications": verifications,
            "deploy": {k: spec[k] for k in ("old", "head") if k in spec} or None}
