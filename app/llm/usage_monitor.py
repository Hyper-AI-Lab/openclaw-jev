"""Per-profile LLM usage logging: requests, tokens, and OpenClaw gateway LLM calls."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import OPENCLAW_HOME, RMP_DATA_DIR
from app.openclaw_transcripts import decode_event, event_columns

logger = logging.getLogger("rmp.llm_usage")

USAGE_PATH = Path(RMP_DATA_DIR) / "llm_usage.json"
CURSOR_PATH = Path(RMP_DATA_DIR) / "llm_usage_scrape_cursor.json"
LOCK_PATH = USAGE_PATH.parent / ".llm_usage.lock"
# OpenClaw 2026.9+ keeps transcripts in SQLite (transcript_events), not session JSONL.
AGENT_DB_PATH = Path(OPENCLAW_HOME) / "agents" / "main" / "agent" / "openclaw-agent.sqlite"

DEFAULT_INPUT_BUDGET_24H = 5_000_000
LIVE_CONTEXT_MAX_TOKENS = 60_000
ABORT_RATE_MAX = 0.10
ABORT_RATE_MIN_ATTEMPTS = 20
RECENT_WINDOW_HOURS = 2
ABORTED_STOP_REASONS = frozenset({"aborted", "error"})
_RMP_TASK_RE = re.compile(
    r"rmp_task_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
# A task's Aura and evaluator sessions; they get no more turns once the task has ended.
_RMP_RUN_RE = re.compile(
    r"rmp_(?:task|verify)_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

# Sources:
#   embed              — RMP vector embedding API calls
#   openclaw_llm       — OpenClaw gateway chat/completions (from SQLite transcripts)
#   openclaw_hook      — RMP /hooks/agent dispatch (triggers gateway work)
#   rate_limit_429     — NVIDIA 429 observed (RMP path)
#   probe              — manual health probes
#   memory_llm         — the IA's direct model calls (app/llm/openai_direct.py)

_SOURCES = (
    "embed",
    "openclaw_llm",
    "openclaw_hook",
    "rate_limit_429",
    "probe",
    "memory_llm",
)
DIRECT_SOURCES = ("memory_llm",)

_UNSET_PROFILE_IDS = frozenset({"nvidia:unknown", "unknown", ""})


def _is_unset_profile(profile_id: Optional[str]) -> bool:
    return str(profile_id or "").strip() in _UNSET_PROFILE_IDS


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
    pid = resolve_usage_profile_id(profile_id, model=model)
    now = ts if ts is not None else time.time()

    try:
        _mutate_store(
            lambda store: _record_into(
                store,
                pid,
                source,
                now,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                model=model,
                is_rate_limit=is_rate_limit,
            )
        )
    except Exception as exc:
        logger.warning("Failed to record LLM usage: %s", exc)


def _record_into(
    store: Dict[str, Any],
    pid: str,
    source: str,
    now: float,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    model: str = "",
    is_rate_limit: bool = False,
) -> None:
    day = _utc_day(now)
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


def _assistant_llm_turn(
    entry: Dict[str, Any],
    *,
    profile_id: Optional[str] = None,
    session_key: str = "",
) -> Optional[Dict[str, Any]]:
    """Usage fields of one assistant LLM message, or None if the entry is not one."""
    if entry.get("type") != "message":
        return None
    msg = entry.get("message") or {}
    if msg.get("role") != "assistant":
        return None
    provider = str(msg.get("provider") or "")
    model = str(msg.get("model") or "")
    if not provider and not model:
        return None
    usage = msg.get("usage") or {}
    input_t = int(usage.get("input") or 0)
    output_t = int(usage.get("output") or 0)
    stop_reason = entry.get("stopReason") or msg.get("stopReason") or ""
    err = msg.get("errorMessage") or ""
    return {
        "msg_id": entry.get("id") or "",
        "pid": resolve_usage_profile_id(
            profile_id, model=model, provider=provider, session_key=session_key
        ),
        "ts": _parse_jsonl_ts(entry),
        "input_tokens": input_t,
        "output_tokens": output_t,
        "total_tokens": int(usage.get("totalTokens") or (input_t + output_t)),
        "model": model,
        "is_rate_limit": stop_reason == "error" and (
            "429" in err or "rate limit" in err.lower() or "too many" in err.lower()
        ),
    }


def _record_turns_into(store: Dict[str, Any], turns: List[Dict[str, Any]]) -> int:
    seen: List[str] = store.setdefault("seen_message_ids", [])
    seen_set = set(seen)
    logged = 0
    for turn in turns:
        msg_id = turn["msg_id"]
        if msg_id:
            if msg_id in seen_set:
                continue
            seen_set.add(msg_id)
            seen.append(msg_id)
        _record_into(
            store,
            turn["pid"],
            "openclaw_llm",
            turn["ts"],
            input_tokens=turn["input_tokens"],
            output_tokens=turn["output_tokens"],
            total_tokens=turn["total_tokens"],
            model=turn["model"],
            is_rate_limit=turn["is_rate_limit"],
        )
        logged += 1
    store["seen_message_ids"] = seen[-10000:]
    return logged


def record_openclaw_jsonl_message(
    entry: Dict[str, Any],
    *,
    profile_id: Optional[str] = None,
    session_key: str = "",
) -> bool:
    """Record one assistant JSONL message for an LLM provider. Returns True if logged."""
    turn = _assistant_llm_turn(entry, profile_id=profile_id, session_key=session_key)
    if turn is None:
        return False
    return bool(_mutate_store(lambda store: _record_turns_into(store, [turn])))


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
    if not session_key:
        return None
    try:
        from app.openclaw_sessions import get_session_entry

        entry = get_session_entry(session_key) or {}
        pin = entry.get("authProfileOverride")
        return str(pin) if pin else None
    except Exception:
        return None


def resolve_usage_profile_id(
    profile_id: Optional[str] = None,
    *,
    model: str = "",
    provider: str = "",
    session_key: str = "",
) -> str:
    """Attribute a turn to openai:default / nvidia:keyN — never nvidia:unknown for known OpenAI."""
    if profile_id and not _is_unset_profile(profile_id):
        return str(profile_id)
    if session_key:
        pinned = _profile_for_session_key(session_key)
        if pinned:
            return pinned
    prov = (provider or "").strip().lower()
    mdl = (model or "").strip().lower()
    if (
        prov == "openai"
        or mdl.startswith("openai/")
        or mdl.startswith("gpt-")
        or mdl.startswith("text-embedding-")
    ):
        return "openai:default"
    if prov == "nvidia" or mdl.startswith("nvidia/"):
        return "nvidia:default"
    if prov:
        return f"{prov}:unknown"
    return "unknown"


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


def _open_agent_db():
    import sqlite3

    return sqlite3.connect(f"file:{AGENT_DB_PATH}?mode=ro", uri=True, timeout=10)


def scrape_openclaw_sessions(limit_events: int = 5000) -> Dict[str, Any]:
    """Record new assistant LLM turns from OpenClaw's SQLite transcripts in the usage store.

    Resumes from the last ``transcript_events`` rowid. The first run starts 24 h back. Events
    created more than 24 h before the previous scrape are skipped: OpenClaw 2026.9.7's migration
    imported months of JSONL history at new rowids, and those turns were counted long ago.
    """
    if not AGENT_DB_PATH.is_file():
        return {"new_events": 0, "scanned_events": 0, "source": "missing"}
    cursor = _read_cursor()
    last_rowid = int(cursor.get("transcript_rowid") or 0)
    since_ms = int(cursor.get("last_scrape_ms") or time.time() * 1000) - 86_400_000
    try:
        db = _open_agent_db()
        try:
            if not last_rowid:
                row = db.execute(
                    "SELECT MIN(rowid) FROM transcript_events WHERE created_at >= ?",
                    (since_ms,),
                ).fetchone()
                last_rowid = max(0, int(row[0] or 1) - 1)
            rows = db.execute(
                f"SELECT e.rowid, e.session_id, {event_columns(db, 'e')}, "
                "(SELECT w.session_key FROM session_windows w WHERE w.session_id = e.session_id) "
                "FROM transcript_events e WHERE e.rowid > ? AND e.created_at >= ? ORDER BY e.rowid LIMIT ?",
                (last_rowid, since_ms, limit_events),
            ).fetchall()
        finally:
            db.close()
    except Exception as exc:
        logger.debug("Could not scrape OpenClaw transcripts: %s", exc)
        return {"new_events": 0, "scanned_events": 0, "source": "error"}

    turns: List[Dict[str, Any]] = []
    for rowid, session_id, event_json, event_zstd, utf8_bytes, session_key in rows:
        last_rowid = int(rowid)
        try:
            entry = json.loads(decode_event(event_json, event_zstd, utf8_bytes))
        except (TypeError, json.JSONDecodeError):
            continue
        turn = _assistant_llm_turn(
            entry,
            profile_id=_profile_for_session_key(session_key or ""),
            session_key=session_key or "",
        )
        if turn is not None:
            turns.append(turn)

    logged = int(_mutate_store(lambda store: _record_turns_into(store, turns)) or 0) if turns else 0
    cursor["transcript_rowid"] = last_rowid
    cursor["last_scrape_ms"] = int(time.time() * 1000)
    _write_cursor(cursor)
    return {"new_events": logged, "scanned_events": len(rows), "source": "sqlite"}


def _session_category(session_key: str, task_kinds: Dict[str, str]) -> str:
    key = (session_key or "").lower()
    if key.endswith(":heartbeat"):
        return "heartbeat"
    if ":rmp_intake_" in key:
        return "intake"
    if ":rmp_verify_" in key:
        return "evaluator"
    match = _RMP_TASK_RE.search(key)
    if match:
        return "canary" if task_kinds.get(match.group(1).lower()) == "canary" else "task"
    if ":cron:" in key:
        return "cron"
    if ":slack:" in key or key.endswith(":main"):
        return "slack_main"
    return "other"


def _task_kinds_sync(task_ids: List[str]) -> Dict[str, str]:
    """task_id -> "canary" | "user", with the same canary rule as the quota broker."""
    if not task_ids:
        return {}
    try:
        from sqlalchemy import create_engine, text

        from app.db.database import DATABASE_URL

        engine = create_engine(DATABASE_URL.replace("+asyncpg", "+psycopg2"))
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT id::text, task_type, goal FROM tasks WHERE id::text = ANY(:ids)"),
                    {"ids": list(task_ids)},
                ).fetchall()
        finally:
            engine.dispose()
    except Exception as exc:
        logger.debug("task kind lookup failed: %s", exc)
        return {}
    kinds: Dict[str, str] = {}
    for task_id, task_type, goal in rows:
        goal_u = (goal or "").upper()
        is_canary = (task_type or "").lower() == "canary" or "RMP CANARY" in goal_u or "MEMORY CANARY" in goal_u
        kinds[str(task_id).lower()] = "canary" if is_canary else "user"
    return kinds


def _ended_task_ids_sync(task_ids: List[str]) -> set:
    """Ids among ``task_ids`` whose task reached a terminal status."""
    if not task_ids:
        return set()
    try:
        from sqlalchemy import create_engine, text

        from app.db.database import DATABASE_URL
        from app.orchestrator.decision_engine import TERMINAL_STATUSES

        engine = create_engine(DATABASE_URL.replace("+asyncpg", "+psycopg2"))
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT id::text FROM tasks WHERE id::text = ANY(:ids) AND status = ANY(:ended)"),
                    {"ids": list(task_ids), "ended": list(TERMINAL_STATUSES)},
                ).fetchall()
        finally:
            engine.dispose()
    except Exception as exc:
        logger.debug("ended task lookup failed: %s", exc)
        return set()
    return {str(task_id).lower() for (task_id,) in rows}


def _may_run_again(session_key: str, ended: set) -> bool:
    """Intake sessions are single-use; a task's sessions end with the task."""
    if "rmp_intake_" in session_key:
        return False
    match = _RMP_RUN_RE.search(session_key)
    return not (match and match.group(1).lower() in ended)


def _zero_attribution() -> Dict[str, int]:
    return {
        "attempts": 0,
        "aborted": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "aborted_prompt_tokens": 0,
    }


def transcript_usage(hours: float = 24, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Per-category LLM usage from OpenClaw's SQLite transcripts, aborted attempts included.

    An aborted attempt records no usage, yet its prompt was sent. ``aborted_prompt_tokens``
    takes that prompt from the next successful call in the same session (retries resend the
    same prompt), else the previous one. It is an upper bound on re-billing: the provider
    does not bill every aborted request.
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    since_ms = now_ms - int(hours * 3600 * 1000)
    report: Dict[str, Any] = {
        "available": False,
        "window_hours": hours,
        "since_ms": since_ms,
        "days": {},
        "by_category": {},
        "totals": _zero_attribution(),
        "abort_rate": 0.0,
        "max_live_context": {"session_key": None, "tokens": 0},
        "direct": {},
    }
    if not AGENT_DB_PATH.is_file():
        return report
    try:
        db = _open_agent_db()
        try:
            rows = db.execute(
                f"SELECT e.session_id, e.seq, {event_columns(db, 'e')}, e.created_at, "
                "(SELECT w.session_key FROM session_windows w WHERE w.session_id = e.session_id) "
                "FROM transcript_events e WHERE e.created_at >= ? AND e.created_at <= ? "
                "ORDER BY e.session_id, e.seq",
                (since_ms, now_ms),
            ).fetchall()
            live = dict(
                db.execute(
                    "SELECT current_session_id, session_key FROM session_nodes "
                    "WHERE archived_at IS NULL AND current_session_id IS NOT NULL"
                ).fetchall()
            )
        finally:
            db.close()
    except Exception as exc:
        logger.debug("transcript usage query failed: %s", exc)
        return report
    report["available"] = True
    report["direct"] = _direct_usage(since_ms, now_ms)

    turns: List[Dict[str, Any]] = []
    for session_id, _seq, event_json, event_zstd, utf8_bytes, created_at, session_key in rows:
        try:
            entry = json.loads(decode_event(event_json, event_zstd, utf8_bytes))
        except (TypeError, json.JSONDecodeError):
            continue
        msg = entry.get("message") or {}
        if entry.get("type") != "message" or msg.get("role") != "assistant":
            continue
        if not msg.get("model") or msg.get("provider") == "openclaw":
            continue
        usage = msg.get("usage") or {}
        stop = str(msg.get("stopReason") or entry.get("stopReason") or "")
        turns.append(
            {
                "session_id": session_id,
                "session_key": session_key or live.get(session_id) or "",
                "day": _utc_day(int(created_at) / 1000.0),
                "aborted": stop in ABORTED_STOP_REASONS,
                "input": int(usage.get("input") or 0),
                "cache_read": int(usage.get("cacheRead") or 0),
                "prompt": int(usage.get("input") or 0)
                + int(usage.get("cacheRead") or 0)
                + int(usage.get("cacheWrite") or 0),
                "output": int(usage.get("output") or 0),
            }
        )

    task_ids = sorted(
        {m.group(1).lower() for t in turns for m in [_RMP_TASK_RE.search(t["session_key"])] if m}
    )
    task_kinds = _task_kinds_sync(task_ids)

    by_session: Dict[str, List[Dict[str, Any]]] = {}
    for turn in turns:
        by_session.setdefault(turn["session_id"], []).append(turn)
    latest_prompt: Dict[str, int] = {}
    for session_id, session_turns in by_session.items():
        pending: List[Dict[str, Any]] = []
        previous = 0
        for turn in session_turns:
            if turn["aborted"]:
                turn["sent"] = previous
                pending.append(turn)
                continue
            if turn["prompt"]:
                for aborted in pending:
                    aborted["sent"] = turn["prompt"]
                pending = []
                previous = turn["prompt"]
                latest_prompt[session_id] = turn["prompt"]

    for turn in turns:
        category = _session_category(turn["session_key"], task_kinds)
        day_bucket = report["days"].setdefault(turn["day"], {}).setdefault(
            category, _zero_attribution()
        )
        for bucket in (
            day_bucket,
            report["by_category"].setdefault(category, _zero_attribution()),
            report["totals"],
        ):
            bucket["attempts"] += 1
            bucket["aborted"] += int(turn["aborted"])
            bucket["input_tokens"] += turn["input"]
            bucket["cache_read_tokens"] += turn["cache_read"]
            bucket["output_tokens"] += turn["output"]
            bucket["aborted_prompt_tokens"] += int(turn.get("sent") or 0)

    attempts = report["totals"]["attempts"]
    report["abort_rate"] = (report["totals"]["aborted"] / attempts) if attempts else 0.0
    # Live means a session that will be billed again, not merely one that is not archived.
    run_tasks = {
        m.group(1).lower()
        for session_id in latest_prompt
        for m in [_RMP_RUN_RE.search(live.get(session_id) or "")]
        if m
    }
    ended = _ended_task_ids_sync(sorted(run_tasks))
    for session_id, tokens in latest_prompt.items():
        key = live.get(session_id)
        if not key or not _may_run_again(key, ended):
            continue
        if tokens > report["max_live_context"]["tokens"]:
            report["max_live_context"] = {"session_key": key, "tokens": tokens}
    return report


def source_tokens_today(source: str) -> int:
    """Today's (UTC) input plus output tokens recorded under ``source``, all profiles."""
    day = (_read_store().get("days") or {}).get(_utc_day()) or {}
    total = 0
    for bucket in (day.get("profiles") or {}).values():
        counts = (bucket.get("by_source") or {}).get(source) or {}
        total += int(counts.get("input_tokens") or 0) + int(counts.get("output_tokens") or 0)
    return total


def _direct_usage(since_ms: int, now_ms: int) -> Dict[str, Dict[str, int]]:
    """Ledger totals of direct (non-OpenClaw) model calls in a window, per source."""
    out: Dict[str, Dict[str, int]] = {}
    for entry in _read_store().get("rolling_24h") or []:
        source = entry.get("source")
        ts = int(entry.get("ts_ms") or 0)
        if source not in DIRECT_SOURCES or not since_ms <= ts <= now_ms:
            continue
        bucket = out.setdefault(source, {"requests": 0, "input_tokens": 0, "output_tokens": 0})
        if not entry.get("is_rate_limit"):
            bucket["requests"] += 1
        bucket["input_tokens"] += int(entry.get("input_tokens") or 0)
        bucket["output_tokens"] += int(entry.get("output_tokens") or 0)
    return out


def _direct_prompt_tokens(report: Optional[Dict[str, Any]]) -> int:
    return sum(int(v.get("input_tokens") or 0) for v in ((report or {}).get("direct") or {}).values())


def _input_budget_24h() -> int:
    try:
        from app.config import load_settings

        return int((load_settings().get("llm_usage") or {}).get("input_budget_24h") or DEFAULT_INPUT_BUDGET_24H)
    except Exception:
        return DEFAULT_INPUT_BUDGET_24H


def _prompt_tokens(totals: Dict[str, int]) -> int:
    return totals["input_tokens"] + totals["cache_read_tokens"] + totals["aborted_prompt_tokens"]


def usage_alerts(
    report: Dict[str, Any],
    *,
    recent: Optional[Dict[str, Any]] = None,
    input_budget: Optional[int] = None,
) -> List[str]:
    """Threshold breaches in a 24 h ``transcript_usage`` report; empty when healthy.

    With ``recent`` (a shorter report), the budget and abort-rate breaches count only while
    they are still happening, so a burn that already stopped does not keep re-alerting until
    it leaves the 24 h window.
    """
    if not report.get("available"):
        return []
    budget = int(input_budget if input_budget is not None else _input_budget_24h())
    totals = report.get("totals") or _zero_attribution()
    recent_totals = (recent or {}).get("totals") or _zero_attribution()
    recent_hours = float((recent or {}).get("window_hours") or 24)
    alerts: List[str] = []

    prompt = _prompt_tokens(totals) + _direct_prompt_tokens(report)
    recent_prompt = _prompt_tokens(recent_totals) + _direct_prompt_tokens(recent)
    still_burning = recent is None or recent_prompt * 24 / recent_hours > budget
    if prompt > budget and still_burning:
        alerts.append(f"24 h prompt tokens {prompt:,} over budget {budget:,}")

    ctx = report.get("max_live_context") or {}
    if int(ctx.get("tokens") or 0) > LIVE_CONTEXT_MAX_TOKENS:
        alerts.append(
            f"live session {ctx.get('session_key')} carries {int(ctx['tokens']):,} context tokens "
            f"(limit {LIVE_CONTEXT_MAX_TOKENS:,})"
        )

    attempts = totals["attempts"]
    rate = float(report.get("abort_rate") or 0.0)
    recent_min = max(5, round(ABORT_RATE_MIN_ATTEMPTS * recent_hours / 24))
    still_aborting = recent is None or (
        recent_totals["attempts"] >= recent_min
        and float(recent.get("abort_rate") or 0.0) > ABORT_RATE_MAX
    )
    if attempts >= ABORT_RATE_MIN_ATTEMPTS and rate > ABORT_RATE_MAX and still_aborting:
        alerts.append(
            f"abort rate {rate:.0%} ({totals['aborted']}/{attempts} attempts) over {ABORT_RATE_MAX:.0%}"
        )
    return alerts


def get_today_load_by_profile() -> Dict[str, Dict[str, int]]:
    """Today's usage totals per profile (no transcript scrape)."""
    store = _read_store()
    today = _utc_day()
    profiles = (store.get("days") or {}).get(today, {}).get("profiles") or {}
    return {
        pid: dict(pdata.get("totals") or _zero_counts())
        for pid, pdata in profiles.items()
    }


def rewrite_unattributed_rolling() -> int:
    """Rewrite rolling_24h nvidia:unknown only when the event has a model. Leave day buckets."""

    def _apply(store: Dict[str, Any]) -> int:
        return _rewrite_rolling_unknown_with_model(store)

    return int(_mutate_store(_apply) or 0)


def _rewrite_rolling_unknown_with_model(store: Dict[str, Any]) -> int:
    changed = 0
    for event in store.get("rolling_24h") or []:
        model = str(event.get("model") or "").strip()
        if not model:
            continue
        raw = str(event.get("profile_id") or "")
        if not _is_unset_profile(raw):
            continue
        new_pid = resolve_usage_profile_id(raw, model=model)
        if new_pid != raw and not _is_unset_profile(new_pid):
            event["profile_id"] = new_pid
            changed += 1
    return changed


def _unattributed_historical_days(store: Dict[str, Any]) -> List[str]:
    days = []
    for day, data in (store.get("days") or {}).items():
        profiles = (data or {}).get("profiles") or {}
        if any(_is_unset_profile(pid) for pid in profiles):
            days.append(str(day))
    return sorted(days)


def get_summary() -> Dict[str, Any]:
    """Return today + rolling-24h usage per profile."""
    scrape_openclaw_sessions()
    rewrite_unattributed_rolling()
    store = _read_store()
    today = _utc_day()
    today_data = (store.get("days") or {}).get(today, {})
    now = time.time()
    cutoff_ms = int((now - 86400) * 1000)

    rolling: Dict[str, Dict[str, int]] = {}
    for event in store.get("rolling_24h") or []:
        if event.get("ts_ms", 0) < cutoff_ms:
            continue
        pid = resolve_usage_profile_id(
            event.get("profile_id"),
            model=str(event.get("model") or ""),
        )
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

    unattributed = _unattributed_historical_days(store)
    transcripts = transcript_usage(hours=24)
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
        "unattributed_historical_days": unattributed,
        "unattributed_note": (
            "Day-bucket totals for these UTC days include nvidia:unknown/unknown "
            "with no stored model id; they were not rewritten. Never invent nvidia:keyN."
            if unattributed
            else None
        ),
        "transcripts_24h": transcripts,
        "transcript_alerts": usage_alerts(
            transcripts, recent=transcript_usage(hours=RECENT_WINDOW_HOURS)
        ),
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
    """Record assistant LLM turns in a JSONL file after start_time."""
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
