"""Per-key NVIDIA usage logging: requests, tokens, and OpenClaw gateway LLM calls."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("rmp.llm_usage")

USAGE_PATH = Path("/root/.openclaw/rmp/data/llm_usage.json")
CURSOR_PATH = Path("/root/.openclaw/rmp/data/llm_usage_scrape_cursor.json")
LOCK_PATH = USAGE_PATH.parent / ".llm_usage.lock"
SESSIONS_DIR = Path("/root/.openclaw/agents/main/sessions")
SESSIONS_JSON = SESSIONS_DIR / "sessions.json"

# Sources:
#   embed              — RMP vector embedding API calls
#   openclaw_llm       — OpenClaw gateway chat/completions (from session JSONL)
#   openclaw_hook      — RMP /hooks/agent dispatch (triggers gateway work)
#   rate_limit_429     — NVIDIA 429 observed (RMP path)
#   probe              — manual health probes

_SOURCES = (
    "embed",
    "openclaw_llm",
    "openclaw_hook",
    "rate_limit_429",
    "probe",
)


def _utc_day(ts: Optional[float] = None) -> str:
    t = ts if ts is not None else time.time()
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def estimate_tokens(text: str) -> int:
    """Rough token estimate when the API does not return usage."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _empty_store() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_ms": 0,
        "days": {},
        "rolling_24h": [],
        "seen_message_ids": [],
    }


def _read_store() -> Dict[str, Any]:
    if not USAGE_PATH.is_file():
        return _empty_store()
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        data.setdefault("days", {})
        data.setdefault("rolling_24h", [])
        data.setdefault("seen_message_ids", [])
        return data
    except Exception:
        return _empty_store()


def _write_store(store: Dict[str, Any]) -> None:
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    store["updated_ms"] = int(time.time() * 1000)
    payload = json.dumps(store, indent=2)
    fd, tmp_path = tempfile.mkstemp(
        dir=USAGE_PATH.parent, prefix=".llm_usage_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, USAGE_PATH)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    try:
        os.chmod(USAGE_PATH, 0o600)
    except OSError:
        pass


def _with_lock(fn):
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        return fn()


def _mutate_store(mutator) -> Any:
    def _run():
        store = _read_store()
        result = mutator(store)
        _write_store(store)
        return result

    return _with_lock(_run)


def _profile_bucket(store: Dict[str, Any], day: str, profile_id: str) -> Dict[str, Any]:
    days = store.setdefault("days", {})
    day_entry = days.setdefault(day, {"profiles": {}, "totals": _zero_counts()})
    profiles = day_entry.setdefault("profiles", {})
    bucket = profiles.setdefault(profile_id, {"by_source": {}, "totals": _zero_counts()})
    bucket.setdefault("by_source", {})
    bucket.setdefault("totals", _zero_counts())
    return bucket


def _zero_counts() -> Dict[str, int]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "rate_limits": 0,
    }


def _add_counts(
    target: Dict[str, int],
    *,
    requests: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    rate_limits: int = 0,
) -> None:
    target["requests"] += requests
    target["input_tokens"] += input_tokens
    target["output_tokens"] += output_tokens
    if total_tokens:
        target["total_tokens"] += total_tokens
    else:
        target["total_tokens"] += input_tokens + output_tokens
    target["rate_limits"] += rate_limits


def record_request(
    profile_id: str,
    source: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    model: str = "",
    is_rate_limit: bool = False,
    ts: Optional[float] = None,
) -> None:
    """Log one API-facing event (including gateway LLM turns scraped from JSONL)."""
    if source not in _SOURCES:
        logger.debug("Unknown usage source %r; recording anyway", source)
    pid = profile_id or "nvidia:unknown"
    now = ts if ts is not None else time.time()
    day = _utc_day(now)

    def _apply(store: Dict[str, Any]) -> None:
        bucket = _profile_bucket(store, day, pid)
        src_bucket = bucket["by_source"].setdefault(
            source, _zero_counts()
        )
        req_inc = 0 if is_rate_limit else 1
        rl_inc = 1 if is_rate_limit else 0
        for target in (src_bucket, bucket["totals"], store["days"][day]["totals"]):
            _add_counts(
                target,
                requests=req_inc,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                rate_limits=rl_inc,
            )

        store.setdefault("rolling_24h", []).append(
            {
                "ts_ms": int(now * 1000),
                "profile_id": pid,
                "source": source,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens or (input_tokens + output_tokens),
                "is_rate_limit": is_rate_limit,
            }
        )
        cutoff_ms = int((now - 86400) * 1000)
        store["rolling_24h"] = [
            e for e in store["rolling_24h"] if e.get("ts_ms", 0) >= cutoff_ms
        ][-5000:]

    try:
        _mutate_store(_apply)
    except Exception as exc:
        logger.warning("Failed to record LLM usage: %s", exc)


def record_openclaw_jsonl_message(
    entry: Dict[str, Any],
    *,
    profile_id: Optional[str] = None,
    session_key: str = "",
) -> bool:
    """Record one assistant JSONL message if it is an NVIDIA LLM call. Returns True if logged."""
    if entry.get("type") != "message":
        return False
    msg = entry.get("message") or {}
    if msg.get("role") != "assistant":
        return False
    if msg.get("provider") != "nvidia":
        return False

    msg_id = entry.get("id") or ""
    if msg_id:
        def _dedupe(store: Dict[str, Any]) -> bool:
            seen: List[str] = store.setdefault("seen_message_ids", [])
            if msg_id in seen:
                return False
            seen.append(msg_id)
            store["seen_message_ids"] = seen[-10000:]
            return True

        if not _mutate_store(_dedupe):
            return False

    usage = msg.get("usage") or {}
    input_t = int(usage.get("input") or 0)
    output_t = int(usage.get("output") or 0)
    total_t = int(usage.get("totalTokens") or (input_t + output_t))
    stop_reason = entry.get("stopReason") or msg.get("stopReason") or ""
    err = msg.get("errorMessage") or ""
    is_rl = stop_reason == "error" and (
        "429" in err or "rate limit" in err.lower() or "too many" in err.lower()
    )

    ts = _parse_jsonl_ts(entry)
    pid = profile_id or _profile_for_session_key(session_key) or "nvidia:unknown"
    record_request(
        pid,
        "openclaw_llm",
        input_tokens=input_t,
        output_tokens=output_t,
        total_tokens=total_t,
        model=str(msg.get("model") or ""),
        is_rate_limit=is_rl,
        ts=ts,
    )
    return True


def _parse_jsonl_ts(entry: Dict[str, Any]) -> float:
    raw = entry.get("timestamp")
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    msg_ts = (entry.get("message") or {}).get("timestamp")
    if msg_ts:
        try:
            return float(msg_ts) / 1000.0
        except Exception:
            pass
    return time.time()


def _profile_for_session_key(session_key: str) -> Optional[str]:
    if not session_key or not SESSIONS_JSON.is_file():
        return None
    try:
        data = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
        entry = data.get(session_key) or {}
        return entry.get("authProfileOverride")
    except Exception:
        return None


def _profile_for_session_id(session_id: str) -> Optional[str]:
    if not session_id or not SESSIONS_JSON.is_file():
        return None
    try:
        data = json.loads(SESSIONS_JSON.read_text(encoding="utf-8"))
        for _key, entry in data.items():
            if (entry or {}).get("sessionId") == session_id:
                return entry.get("authProfileOverride")
    except Exception:
        pass
    return None


def _read_cursor() -> Dict[str, Any]:
    if not CURSOR_PATH.is_file():
        return {"files": {}}
    try:
        return json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"files": {}}


def _write_cursor(cursor: Dict[str, Any]) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_PATH.write_text(json.dumps(cursor, indent=2), encoding="utf-8")


def scrape_openclaw_sessions(limit_files: int = 200) -> Dict[str, Any]:
    """Scan session JSONL files for new NVIDIA LLM usage (heartbeats, Slack, etc.)."""
    cursor = _read_cursor()
    file_cursors: Dict[str, Any] = cursor.setdefault("files", {})
    logged = 0
    scanned = 0

    jsonl_files = sorted(
        SESSIONS_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit_files]

    session_key_by_id: Dict[str, str] = {}
    if SESSIONS_JSON.is_file():
        try:
            for sk, entry in json.loads(
                SESSIONS_JSON.read_text(encoding="utf-8")
            ).items():
                sid = (entry or {}).get("sessionId")
                if sid:
                    session_key_by_id[sid] = sk
        except Exception:
            pass

    for path in jsonl_files:
        scanned += 1
        path_str = str(path)
        stat = path.stat()
        prev = file_cursors.get(path_str, {})
        offset = int(prev.get("offset", 0))
        if prev.get("size") == stat.st_size and prev.get("mtime") == stat.st_mtime:
            continue
        if offset > stat.st_size:
            offset = 0

        session_id = path.stem
        session_key = session_key_by_id.get(session_id, "")
        profile_id = _profile_for_session_id(session_id)

        try:
            with open(path, "r", encoding="utf-8") as handle:
                if offset:
                    handle.seek(offset)
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record_openclaw_jsonl_message(
                        entry,
                        profile_id=profile_id,
                        session_key=session_key,
                    ):
                        logged += 1
                new_offset = handle.tell()
        except OSError as exc:
            logger.debug("Could not scrape %s: %s", path, exc)
            continue

        file_cursors[path_str] = {
            "offset": new_offset,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }

    cursor["last_scrape_ms"] = int(time.time() * 1000)
    _write_cursor(cursor)
    return {"scanned_files": scanned, "new_events": logged}


def get_today_load_by_profile() -> Dict[str, Dict[str, int]]:
    """Today's usage totals per profile (no JSONL scrape)."""
    store = _read_store()
    today = _utc_day()
    profiles = (store.get("days") or {}).get(today, {}).get("profiles") or {}
    return {
        pid: dict(pdata.get("totals") or _zero_counts())
        for pid, pdata in profiles.items()
    }


def get_summary() -> Dict[str, Any]:
    """Return today + rolling-24h usage per profile."""
    scrape_openclaw_sessions()
    store = _read_store()
    today = _utc_day()
    today_data = (store.get("days") or {}).get(today, {})
    now = time.time()
    cutoff_ms = int((now - 86400) * 1000)

    rolling: Dict[str, Dict[str, int]] = {}
    for event in store.get("rolling_24h") or []:
        if event.get("ts_ms", 0) < cutoff_ms:
            continue
        pid = event.get("profile_id") or "nvidia:unknown"
        bucket = rolling.setdefault(pid, _zero_counts())
        if event.get("is_rate_limit"):
            bucket["rate_limits"] += 1
        else:
            bucket["requests"] += 1
        bucket["input_tokens"] += int(event.get("input_tokens") or 0)
        bucket["output_tokens"] += int(event.get("output_tokens") or 0)
        bucket["total_tokens"] += int(event.get("total_tokens") or 0)

    by_source_today: Dict[str, Dict[str, int]] = {}
    for pid, pdata in (today_data.get("profiles") or {}).items():
        for src, counts in (pdata.get("by_source") or {}).items():
            agg = by_source_today.setdefault(src, _zero_counts())
            for k in agg:
                agg[k] += int((counts or {}).get(k) or 0)

    return {
        "updated_ms": store.get("updated_ms"),
        "utc_day": today,
        "today_totals": today_data.get("totals") or _zero_counts(),
        "today_by_profile": {
            pid: pdata.get("totals") or _zero_counts()
            for pid, pdata in (today_data.get("profiles") or {}).items()
        },
        "today_by_source": by_source_today,
        "rolling_24h_by_profile": rolling,
        "profiles_configured": [p for p, _ in _load_profile_ids()],
    }


def _load_profile_ids():
    from app.llm.quota_broker import _load_env_keys

    return _load_env_keys()


def record_jsonl_usage_since(
    jsonl_path: str,
    start_time_ms: float,
    *,
    profile_id: Optional[str] = None,
    session_key: str = "",
) -> int:
    """Record all NVIDIA assistant turns in a JSONL file after start_time."""
    if not os.path.exists(jsonl_path):
        return 0
    count = 0
    start_sec = start_time_ms / 1000.0
    try:
        with open(jsonl_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _parse_jsonl_ts(entry) <= start_sec:
                    continue
                if record_openclaw_jsonl_message(
                    entry,
                    profile_id=profile_id,
                    session_key=session_key,
                ):
                    count += 1
    except OSError:
        pass
    return count
