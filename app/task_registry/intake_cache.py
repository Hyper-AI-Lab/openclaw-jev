"""File-backed intake decision cache (survives API restarts)."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.config import RMP_DATA_DIR

CACHE_PATH = Path(RMP_DATA_DIR) / "intake_cache.json"
LOCK_PATH = CACHE_PATH.parent / ".intake_cache.lock"

_lock = threading.Lock()


def _read_store() -> Dict[str, Any]:
    if not CACHE_PATH.exists():
        return {"entries": {}}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "entries" in data:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"entries": {}}


def _write_store(store: Dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(store, indent=2)
    fd, tmp = tempfile.mkstemp(
        dir=CACHE_PATH.parent, prefix=".intake_cache_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CACHE_PATH)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def get_cached(key: str, max_age_sec: int) -> Optional[Dict[str, Any]]:
    with _lock:
        with open(LOCK_PATH, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                store = _read_store()
                entry = (store.get("entries") or {}).get(key)
                if not entry:
                    return None
                ts, value = entry.get("ts", 0), entry.get("value")
                if time.time() - float(ts) > max_age_sec:
                    return None
                return value if isinstance(value, dict) else None
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def set_cached(key: str, value: Dict[str, Any]) -> None:
    with _lock:
        with open(LOCK_PATH, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                store = _read_store()
                entries = store.setdefault("entries", {})
                entries[key] = {"ts": time.time(), "value": value}
                # prune old keys
                cutoff = time.time() - 3600
                entries = {
                    k: v
                    for k, v in entries.items()
                    if float(v.get("ts", 0)) >= cutoff
                }
                store["entries"] = entries
                _write_store(store)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
