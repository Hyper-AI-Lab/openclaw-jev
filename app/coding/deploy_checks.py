"""Checks after a self-deploy restarts services: RMP's health, the gateway, readiness and a canary."""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Union

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


def readiness_report() -> Dict[str, str]:
    """Every readiness check by name, with its status: "pass", "warn" or "fail"."""
    from app.config import get_api_key

    request = urllib.request.Request(f"{RMP}/api/production/readiness", headers={"X-RMP-API-Key": get_api_key() or ""})
    with urllib.request.urlopen(request, timeout=90) as response:
        report = json.load(response)
    return {check.get("name"): check.get("status") for check in report.get("checks") or []}


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


def run(live: Path, restarted: Iterable[str], baseline: Union[Dict[str, str], List[str]], *,
        ignore: Iterable[str] = ()) -> Dict[str, Any]:
    """``failure`` is None when everything passed. Readiness is compared like with like with ``baseline``, the report
    taken before the change: a check that passed then and fails now fails the deploy; one that fails now and was absent
    or a warning then (a check the change adds or tightens) is listed in ``new_failing`` and never fails it; one that
    failed then too is ignored, and so is every name in ``ignore``.

    A run's state from the deploy code before this one holds the names failing then as a list; every other check counts
    as passed then, as that code compared."""
    if not _wait_ok(f"{RMP}/health", 120):
        return {"failure": "RMP's /health did not answer within 2 minutes", "canary": None}
    if "openclaw-gateway" in restarted and not _wait_ok(GATEWAY_READY, 300):
        return {"failure": "the gateway was not ready within 5 minutes", "canary": None}
    try:
        now = readiness_report()
    except Exception as exc:
        return {"failure": f"the readiness report failed ({exc})", "canary": None}
    if isinstance(baseline, list):
        baseline = {name: "fail" if name in baseline else "pass" for name in now}
    ignored = set(ignore)
    failing = sorted(name for name, status in now.items() if status == "fail" and name not in ignored)
    regressions = [name for name in failing if baseline.get(name) == "pass"]
    if regressions:
        return {"failure": f"readiness checks failed: {', '.join(regressions)}", "canary": None}
    result = canary(live)
    if result not in ("ok", "skipped"):
        return {"failure": result, "canary": None}
    return {"failure": None, "canary": result,
            "new_failing": [name for name in failing if baseline.get(name, "warn") == "warn"]}
