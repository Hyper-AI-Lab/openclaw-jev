"""Checks after a self-deploy restarts services: RMP's health, the gateway, readiness and a canary."""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List

RMP = "http://127.0.0.1:8000"
GATEWAY_READY = "http://127.0.0.1:18789/readyz"
CANARY_TRIES = 5
CANARY_WAIT_SEC = 30


def _status(url: str, timeout: float = 10) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def _wait_ok(url: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _status(url) == 200:
            return True
        time.sleep(2)
    return False


def readiness_failures() -> List[str]:
    from app.config import get_api_key

    request = urllib.request.Request(f"{RMP}/api/production/readiness", headers={"X-RMP-API-Key": get_api_key() or ""})
    with urllib.request.urlopen(request, timeout=90) as response:
        report = json.load(response)
    return sorted(check.get("name") for check in report.get("checks") or [] if check.get("status") == "fail")


def canary(live: Path) -> str:
    """"ok", "skipped" while user tasks keep the canary from running, or the failure line."""
    for _ in range(CANARY_TRIES):
        run = subprocess.run(["bash", str(live / "ops" / "canary.sh")], cwd=live, capture_output=True, text=True, timeout=600)
        if "CANARY OK" in run.stdout:
            return "ok"
        if "CANARY SKIP" not in run.stdout:
            return next((line for line in run.stdout.splitlines() if line.startswith("CANARY")), f"the canary exited {run.returncode}")
        time.sleep(CANARY_WAIT_SEC)
    return "skipped"


def run(live: Path, restarted: Iterable[str], baseline: Iterable[str] = ()) -> Dict[str, Any]:
    """``failure`` is None when everything passed; readiness checks failing before the deploy don't count."""
    if not _wait_ok(f"{RMP}/health", 120):
        return {"failure": "RMP's /health did not answer within 2 minutes", "canary": None}
    if "openclaw-gateway" in restarted and not _wait_ok(GATEWAY_READY, 300):
        return {"failure": "the gateway was not ready within 5 minutes", "canary": None}
    try:
        new = sorted(set(readiness_failures()) - set(baseline))
    except Exception as exc:
        return {"failure": f"the readiness report failed ({exc})", "canary": None}
    if new:
        return {"failure": f"readiness checks failed: {', '.join(new)}", "canary": None}
    result = canary(live)
    if result not in ("ok", "skipped"):
        return {"failure": result, "canary": None}
    return {"failure": None, "canary": result}
