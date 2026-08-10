"""Optional auto-restart for managed Moltbook scanners."""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from typing import Any, Dict, Optional

from app.config import load_settings
from app.scanners.catalog import ScannerDefinition

logger = logging.getLogger("rmp.scanners.lifecycle")


def _production_config() -> dict:
    return load_settings().get("production", {})


def is_scanner_auto_restart_enabled() -> bool:
    return bool(_production_config().get("scanner_auto_restart"))


def is_scanner_managed(scanner_id: str) -> bool:
    cfg = _production_config()
    managed = cfg.get("scanner_managed_ids") or []
    if not managed:
        return False
    return scanner_id in managed


async def maybe_restart_scanner(
    scanner_id: str,
    definition: ScannerDefinition,
    *,
    reason: str = "stale",
) -> Optional[Dict[str, Any]]:
    """Start scanner script in background if managed and auto-restart enabled."""
    if not is_scanner_auto_restart_enabled():
        return None
    if not is_scanner_managed(scanner_id):
        return None

    script = definition.script_paths[0] if definition.script_paths else None
    if not script or not os.path.isfile(script):
        logger.warning("Cannot restart %s: script missing", scanner_id)
        return None

    log_path = definition.log_path or f"/tmp/rmp_scanner_{scanner_id}.log"
    try:
        with open(log_path, "a") as logf:
            logf.write(f"\n[{datetime.utcnow().isoformat()}Z] RMP auto-restart ({reason})\n")
            proc = subprocess.Popen(
                ["node", script],
                stdout=logf,
                stderr=subprocess.STDOUT,
                cwd=os.path.dirname(script) or "/root",
                start_new_session=True,
            )
        logger.info("Restarted scanner %s pid=%s", scanner_id, proc.pid)
        return {"scanner_id": scanner_id, "pid": proc.pid, "script": script, "reason": reason}
    except Exception as e:
        logger.exception("Failed to restart scanner %s: %s", scanner_id, e)
        return None
