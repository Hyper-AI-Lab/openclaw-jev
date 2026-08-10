"""Detect Moltbook scanner OS processes via /proc."""
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app.scanners.catalog import match_scanner_id

logger = logging.getLogger("rmp.scanners.monitor")


@dataclass
class RunningScanner:
    scanner_id: str
    pid: int
    cmdline: str
    script_path: Optional[str] = None


def _read_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\0", b" ").decode("utf-8", errors="ignore").strip()
    except OSError:
        return ""


def _read_start_time(pid: int) -> Optional[datetime]:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # field 22 is starttime in clock ticks after boot
        parts = stat.split()
        if len(parts) < 22:
            return None
        starttime_ticks = int(parts[21])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        hz = 100  # USER_HZ on Linux
        started_secs_ago = uptime - (starttime_ticks / hz)
        return datetime.utcfromtimestamp(datetime.utcnow().timestamp() - started_secs_ago)
    except Exception:
        return None


def scan_running_processes() -> List[RunningScanner]:
    """Return all running node processes that match the Moltbook scanner catalog."""
    found: List[RunningScanner] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return found

    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmdline = _read_cmdline(pid)
        if not cmdline:
            continue
        scanner_id = match_scanner_id(cmdline)
        if not scanner_id:
            continue
        script_path = None
        for token in cmdline.split():
            if token.endswith(".js") and "moltbook" in token.lower():
                script_path = token
                break
        found.append(
            RunningScanner(
                scanner_id=scanner_id,
                pid=pid,
                cmdline=cmdline,
                script_path=script_path,
            )
        )
    return found


def running_by_scanner_id() -> Dict[str, RunningScanner]:
    """At most one running instance per scanner_id (highest PID wins)."""
    grouped: Dict[str, RunningScanner] = {}
    for proc in scan_running_processes():
        existing = grouped.get(proc.scanner_id)
        if not existing or proc.pid > existing.pid:
            grouped[proc.scanner_id] = proc
    return grouped


def log_file_mtime(log_path: str) -> Optional[datetime]:
    try:
        p = Path(log_path)
        if p.exists():
            return datetime.utcfromtimestamp(p.stat().st_mtime)
    except OSError:
        pass
    return None


def process_start_time(pid: int) -> Optional[datetime]:
    return _read_start_time(pid)
