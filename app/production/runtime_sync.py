"""Detect when on-disk code is newer than the running API/worker processes.

Long-lived Python processes keep imported modules in memory. After editing
``app/`` (or the adapter plugin) without a restart, new imports fail with
ImportError while older code paths may still appear healthy. Boot stamps
written at process start make that mismatch detectable and remediable.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.config import RMP_DATA_DIR, RMP_ROOT

BOOT_DIR = Path(RMP_DATA_DIR)


def _boot_path(service: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in service)
    return BOOT_DIR / f"runtime_boot_{safe}.json"


def mark_runtime_boot(service: str) -> Path:
    """Record that ``service`` just loaded current on-disk code."""
    BOOT_DIR.mkdir(parents=True, exist_ok=True)
    path = _boot_path(service)
    payload = {
        "service": service,
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "started_at_unix": time.time(),
        "rmp_root": str(RMP_ROOT),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def _read_boot(service: str) -> Optional[dict]:
    path = _boot_path(service)
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def watched_code_paths(root: Optional[Path] = None) -> List[Path]:
    base = Path(root or RMP_ROOT)
    out: List[Path] = []
    app_dir = base / "app"
    if app_dir.is_dir():
        out.extend(p for p in app_dir.rglob("*.py") if p.is_file())
    worker = base / "worker.py"
    if worker.is_file():
        out.append(worker)
    plugin_dir = base / "plugins" / "rmp_adapter"
    if plugin_dir.is_dir():
        out.extend(p for p in plugin_dir.rglob("*.js") if p.is_file())
    seen = set()
    unique: List[Path] = []
    for p in out:
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def newest_code_mtime(root: Optional[Path] = None) -> float:
    newest = 0.0
    for path in watched_code_paths(root):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def runtime_sync_status(
    services: Sequence[str] = ("rmp-api", "rmp-worker"),
    *,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return sync status for each service vs on-disk watched code."""
    code_mtime = newest_code_mtime(root)
    services_out: Dict[str, Any] = {}
    stale_services: List[str] = []
    missing_services: List[str] = []

    for service in services:
        boot = _read_boot(service)
        if not boot:
            missing_services.append(service)
            services_out[service] = {"status": "missing_boot_stamp"}
            continue
        started = float(boot.get("started_at_unix") or 0)
        # 2s skew tolerance for filesystem timestamp granularity
        is_stale = bool(code_mtime and started and code_mtime > started + 2.0)
        if is_stale:
            stale_services.append(service)
        services_out[service] = {
            "status": "stale" if is_stale else "ok",
            "started_at": boot.get("started_at"),
            "started_at_unix": started,
            "pid": boot.get("pid"),
        }

    if missing_services and not stale_services:
        overall = "missing"
    elif stale_services:
        overall = "stale"
    else:
        overall = "ok"

    return {
        "status": overall,
        "code_mtime": code_mtime,
        "stale_services": stale_services,
        "missing_services": missing_services,
        "services": services_out,
    }
