"""In-process Prometheus-style counters (file-backed for multi-process workers)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List

COUNTERS = (
    "task_created",
    "task_completed",
    "task_failed",
    "stale_detected",
    "cron_reconcile",
    "intake_decided",
    "intake_skipped",
    "intake_attached",
    "intake_wait",
    "intake_spawn",
    "intake_workflow_ok",
    "intake_degraded",
)

_DEFAULT_METRICS_PATH = "/root/.openclaw/rmp/data/metrics_counters.json"
_MAX_INTAKE_LATENCY_SAMPLES = 20
_lock = threading.Lock()


def _metrics_path() -> Path:
    return Path(os.environ.get("RMP_METRICS_PATH", _DEFAULT_METRICS_PATH))


def _empty() -> Dict[str, int]:
    return {name: 0 for name in COUNTERS}


def _load_raw() -> dict:
    path = _metrics_path()
    if not path.exists():
        return {"counters": _empty(), "intake_latency_samples": []}
    try:
        raw = json.loads(path.read_text())
        if isinstance(raw, dict) and "counters" in raw:
            return raw
        counters = _empty()
        for name in COUNTERS:
            counters[name] = int(raw.get(name, 0))
        return {"counters": counters, "intake_latency_samples": []}
    except Exception:
        return {"counters": _empty(), "intake_latency_samples": []}


def _load() -> Dict[str, int]:
    return dict(_load_raw().get("counters") or _empty())


def _save(counters: Dict[str, int], latency_samples: List[int]) -> None:
    path = _metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "counters": counters,
        "intake_latency_samples": latency_samples[-_MAX_INTAKE_LATENCY_SAMPLES:],
        "updated_at": int(time.time()),
    }
    path.write_text(json.dumps(payload, indent=0) + "\n")


def inc(name: str, delta: int = 1) -> None:
    if name not in COUNTERS:
        return
    with _lock:
        raw = _load_raw()
        counters = raw.get("counters") or _empty()
        counters[name] = counters.get(name, 0) + delta
        _save(counters, list(raw.get("intake_latency_samples") or []))


def record_intake_run(*, path: str, latency_ms: int, confidence: int) -> None:
    """Record intake completion path and latency sample."""
    with _lock:
        raw = _load_raw()
        counters = raw.get("counters") or _empty()
        if path == "workflow":
            counters["intake_workflow_ok"] = counters.get("intake_workflow_ok", 0) + 1
        else:
            counters["intake_degraded"] = counters.get("intake_degraded", 0) + 1
        samples = list(raw.get("intake_latency_samples") or [])
        samples.append(int(latency_ms))
        _save(counters, samples)


def get_intake_latency_samples() -> List[int]:
    with _lock:
        return list(_load_raw().get("intake_latency_samples") or [])


def get_counters() -> Dict[str, int]:
    with _lock:
        return _load()


def format_prometheus() -> str:
    counters = get_counters()
    lines = [
        "# HELP rmp_info RMP metrics exposition",
        "# TYPE rmp_info gauge",
        "rmp_info 1",
    ]
    for name in COUNTERS:
        metric = f"rmp_{name}_total"
        lines.append(f"# TYPE {metric} counter")
        lines.append(f"{metric} {counters.get(name, 0)}")
    samples = get_intake_latency_samples()
    if samples:
        lines.append("# TYPE rmp_intake_latency_ms_last gauge")
        lines.append(f"rmp_intake_latency_ms_last {samples[-1]}")
    return "\n".join(lines) + "\n"
