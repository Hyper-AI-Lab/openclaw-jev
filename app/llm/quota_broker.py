"""Proactive LLM dispatch gate: pacing, multi-key cooldowns, short backoff."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from app.config import AUTH_PROFILES_PATH as _AUTH_PROFILES, RMP_DATA_DIR

logger = logging.getLogger("rmp.llm_quota")

AUTH_PROFILES_PATH = Path(_AUTH_PROFILES)
STATE_PATH = Path(RMP_DATA_DIR) / "llm_quota.json"
LOCK_PATH = STATE_PATH.parent / ".llm_quota.lock"
OPENCLAW_ENV_PATH = Path("/etc/openclaw/openclaw.env")
OPENCLAW_STATE_DB = Path("/root/.openclaw/state/openclaw.sqlite")
_AUTH_STORE_KEY = "authProfiles.store"
_AUTH_STATE_KEY = "authProfiles.state"
# None = auto (SQLite when live OpenClaw 2026.9+ store exists). Tests set False.
USE_SQLITE_AUTH: Optional[bool] = None

# Shorter than OpenClaw defaults (1m/5m/25m/1h) — prefer rotating keys.
COOLDOWN_STEPS_SEC = (15, 30, 60, 120)
DEFAULT_MIN_INTERVAL_SEC = 5.0
DEFAULT_MAX_WAIT_SEC = 1800.0
DEFAULT_MAX_CONCURRENT = 4
# Intake runs in its own lane, outside max_concurrent, so it never waits behind Aura's runs.
INTAKE_LANE_SLOTS = 1
CANARY_MAX_WAIT_SEC = 60.0
RESERVE_WAIT_LOG_SEC = 2.0

RMP_TASK_SESSION_RE = re.compile(
    r"rmp_task_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "compensated", "stopped_by_user", "cancelled"}
)
DEFAULT_STALE_SLOT_MS = 90 * 60 * 1000
# Canary/system/intake sessions must not pin LLM slots for long — they starve user work.
CANARY_STALE_SLOT_MS = 6 * 60 * 1000
# Direct memory-model calls last at most minutes; an older slot belongs to a dead process.
MEMORY_LANE_STALE_MS = 10 * 60 * 1000
# A recall waiter that stopped polling this long ago no longer holds enrichment back.
MEMORY_RECALL_WAIT_MS = 30 * 1000

_lock = asyncio.Lock()
_state_lock = threading.Lock()

T = TypeVar("T")


@contextmanager
def _file_state_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _write_state_unlocked(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2)
    fd, tmp_path = tempfile.mkstemp(
        dir=STATE_PATH.parent, prefix=".llm_quota_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, STATE_PATH)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    try:
        os.chmod(STATE_PATH, 0o600)
    except OSError:
        pass


def _mutate_state(mutator: Callable[[Dict[str, Any]], T]) -> T:
    with _state_lock:
        with _file_state_lock():
            state = _read_state()
            result = mutator(state)
            _write_state_unlocked(state)
            return result


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _sqlite_auth_available() -> bool:
    db = OPENCLAW_STATE_DB
    if not db.is_file():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT 1 FROM config_machine_state WHERE state_key = ? LIMIT 1",
                (_AUTH_STORE_KEY,),
            ).fetchone()
            return row is not None
        finally:
            con.close()
    except Exception:
        return False


def _use_sqlite_auth() -> bool:
    if USE_SQLITE_AUTH is not None:
        return bool(USE_SQLITE_AUTH)
    if Path(AUTH_PROFILES_PATH).resolve() != Path(_AUTH_PROFILES).resolve():
        return False
    return _sqlite_auth_available()


def _read_machine_state(key: str) -> Optional[Dict[str, Any]]:
    con = sqlite3.connect(f"file:{OPENCLAW_STATE_DB}?mode=ro", uri=True, timeout=10)
    try:
        row = con.execute(
            "SELECT value_json FROM config_machine_state WHERE state_key = ?",
            (key,),
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return None
    data = json.loads(row[0])
    return data if isinstance(data, dict) else None


def _write_machine_state(key: str, value: Dict[str, Any]) -> None:
    payload = json.dumps(value, separators=(",", ":"))
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(OPENCLAW_STATE_DB), timeout=10)
    try:
        con.execute("PRAGMA busy_timeout = 10000")
        con.execute(
            "INSERT INTO config_machine_state(state_key, value_json, updated_at_ms) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(state_key) DO UPDATE SET "
            "value_json = excluded.value_json, updated_at_ms = excluded.updated_at_ms",
            (key, payload, now_ms),
        )
        con.commit()
    finally:
        con.close()


def _retire_legacy_auth_json() -> None:
    path = AUTH_PROFILES_PATH
    if not path.is_file():
        return
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = path.with_name(f"{path.name}.retired-{stamp}")
    path.replace(dest)
    logger.info("Retired leftover %s → %s (OpenClaw 2026.9 reads SQLite)", path.name, dest.name)


def load_openclaw_auth_blob() -> Dict[str, Any]:
    """Combined auth blob: profiles + lastGood + usageStats (JSON or SQLite)."""
    if _use_sqlite_auth():
        store = _read_machine_state(_AUTH_STORE_KEY) or {}
        state = _read_machine_state(_AUTH_STATE_KEY) or {}
        return {
            "version": store.get("version", 1),
            "profiles": dict(store.get("profiles") or {}),
            "lastGood": dict(state.get("lastGood") or {}),
            "usageStats": dict(state.get("usageStats") or {}),
        }
    if AUTH_PROFILES_PATH.is_file():
        try:
            data = json.loads(AUTH_PROFILES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("profiles", {})
                data.setdefault("lastGood", {})
                data.setdefault("usageStats", {})
                return data
        except Exception:
            pass
    return {"version": 1, "profiles": {}, "lastGood": {}, "usageStats": {}}


def save_openclaw_auth_blob(store: Dict[str, Any]) -> None:
    """Persist combined auth blob to SQLite (2026.9+) or legacy JSON."""
    if _use_sqlite_auth():
        existing_store = _read_machine_state(_AUTH_STORE_KEY) or {"version": 1, "profiles": {}}
        existing_state = _read_machine_state(_AUTH_STATE_KEY) or {
            "version": 1,
            "lastGood": {},
            "usageStats": {},
        }
        existing_store["profiles"] = store.get("profiles") or {}
        existing_store["version"] = store.get("version", existing_store.get("version", 1))
        existing_state["lastGood"] = store.get("lastGood") or {}
        existing_state["usageStats"] = store.get("usageStats") or {}
        _write_machine_state(_AUTH_STORE_KEY, existing_store)
        _write_machine_state(_AUTH_STATE_KEY, existing_state)
        _retire_legacy_auth_json()
        return
    AUTH_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(AUTH_PROFILES_PATH, store)


@dataclass
class QuotaConfig:
    provider: str = "nvidia"
    min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC
    max_wait_sec: float = DEFAULT_MAX_WAIT_SEC
    cooldown_steps_sec: tuple = COOLDOWN_STEPS_SEC
    rotation_mode: str = "balanced"  # balanced | lru
    max_concurrent: int = DEFAULT_MAX_CONCURRENT

    @classmethod
    def from_settings(cls, settings: Optional[Dict[str, Any]] = None) -> "QuotaConfig":
        cfg = (settings or {}).get("llm_quota") or {}
        steps = cfg.get("cooldown_steps_sec") or list(COOLDOWN_STEPS_SEC)
        return cls(
            provider=cfg.get("provider", "nvidia"),
            min_interval_sec=float(cfg.get("min_interval_sec", DEFAULT_MIN_INTERVAL_SEC)),
            max_wait_sec=float(cfg.get("max_wait_sec", DEFAULT_MAX_WAIT_SEC)),
            cooldown_steps_sec=tuple(int(s) for s in steps),
            rotation_mode=str(cfg.get("rotation_mode", "balanced")),
            max_concurrent=int(cfg.get("max_concurrent", DEFAULT_MAX_CONCURRENT)),
        )


def _load_env_keys() -> List[tuple[str, str]]:
    """Return (profile_id, api_key) for NVIDIA_API_KEY, NVIDIA_API_KEY_2, ..."""
    values: Dict[str, str] = {}
    if OPENCLAW_ENV_PATH.is_file():
        for line in OPENCLAW_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip()
    for env_name, val in os.environ.items():
        if env_name.startswith("NVIDIA_API_KEY") and val.strip():
            values.setdefault(env_name, val.strip())

    ordered_names = ["NVIDIA_API_KEY"] + [
        f"NVIDIA_API_KEY_{i}" for i in range(2, 10)
    ]
    profiles: List[tuple[str, str]] = []
    for idx, name in enumerate(ordered_names):
        key = values.get(name, "").strip()
        if not key:
            continue
        profile_id = "nvidia:default" if idx == 0 else f"nvidia:key{idx + 1}"
        profiles.append((profile_id, key))
    return profiles


def api_key_for_profile(profile_id: str) -> str:
    for pid, key in _load_env_keys():
        if pid == profile_id:
            return key
    keys = _load_env_keys()
    if keys:
        return keys[0][1]
    return _read_env_value("NVIDIA_API_KEY")


def _read_env_value(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if val:
        return val
    if OPENCLAW_ENV_PATH.is_file():
        for line in OPENCLAW_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return ""


def _read_state() -> Dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"keys": {}, "global": {"last_dispatch_ms": 0}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"keys": {}, "global": {"last_dispatch_ms": 0}}


def _write_state(state: Dict[str, Any]) -> None:
    def _replace(current: Dict[str, Any]) -> None:
        current.clear()
        current.update(state)

    _mutate_state(_replace)


def _now_ms() -> float:
    return time.time() * 1000


def _profile_load_score(totals: Dict[str, int], in_flight: int = 0) -> float:
    """Lower is better. Weight tokens and in-flight agent slots."""
    requests = int(totals.get("requests") or 0)
    tokens = int(totals.get("total_tokens") or 0)
    return requests + (tokens / 5000.0) + (in_flight * 50.0)


def _slot_kind(slot: Dict[str, Any]) -> str:
    return str(slot.get("kind") or "") or classify_slot_kind(slot.get("session_key"))


def _active_slot_count(state: Dict[str, Any]) -> int:
    """Slots under max_concurrent (the intake lane has its own cap)."""
    slots = (state.get("global") or {}).get("active_slots") or {}
    return sum(1 for slot in slots.values() if _slot_kind(slot) != "intake")


def _count_intake_slots(state: Dict[str, Any]) -> int:
    slots = (state.get("global") or {}).get("active_slots") or {}
    return sum(1 for slot in slots.values() if _slot_kind(slot) == "intake")


def _existing_session_slot(
    state: Dict[str, Any], session_key: str
) -> Optional[tuple[str, str]]:
    if not session_key:
        return None
    by_session = (state.get("global") or {}).get("session_slots") or {}
    slot_id = by_session.get(session_key)
    if not slot_id:
        return None
    slot = ((state.get("global") or {}).get("active_slots") or {}).get(slot_id)
    if not slot:
        return None
    return str(slot.get("profile_id") or ""), str(slot_id)


def _today_usage_by_profile() -> Dict[str, Dict[str, int]]:
    try:
        from app.llm.usage_monitor import get_today_load_by_profile

        return get_today_load_by_profile()
    except Exception:
        return {}


def _pick_key(
    state: Dict[str, Any],
    profile_ids: List[str],
    now_ms: float,
    *,
    rotation_mode: str = "balanced",
) -> Optional[str]:
    usage = _today_usage_by_profile() if rotation_mode == "balanced" else {}
    available: List[tuple[float, float, int, str]] = []
    for pid in profile_ids:
        entry = state.get("keys", {}).get(pid, {})
        until = float(entry.get("cooldown_until_ms", 0) or 0)
        if until > now_ms:
            continue
        last_used = float(entry.get("last_used_ms", 0) or 0)
        dispatch_count = int(entry.get("dispatch_count", 0) or 0)
        in_flight = int(entry.get("in_flight", 0) or 0)
        totals = usage.get(pid) or {}
        if rotation_mode == "balanced":
            score = _profile_load_score(totals, in_flight)
        else:
            score = last_used
        available.append((score, last_used, dispatch_count, pid))
    if available:
        available.sort(key=lambda x: (x[0], x[1], x[2]))
        return available[0][3]
    return None  # all cooling — caller waits for soonest


def _soonest_ready_ms(
    state: Dict[str, Any],
    profile_ids: List[str],
    now_ms: float,
    min_interval_sec: float,
) -> float:
    """Per-key pacing only — separate accounts have independent RPM buckets."""
    waits = [0.0]
    min_gap_ms = min_interval_sec * 1000
    for pid in profile_ids:
        entry = state.get("keys", {}).get(pid, {})
        until = float(entry.get("cooldown_until_ms", 0) or 0)
        if until > now_ms:
            waits.append(until - now_ms)
            continue
        last_used = float(entry.get("last_used_ms", 0) or 0)
        if last_used > 0:
            waits.append(max(0.0, (last_used + min_gap_ms) - now_ms))
    return max(waits)


def seconds_until_any_key_ready() -> float:
    """Seconds until at least one NVIDIA key is off cooldown (0 if any is ready)."""
    state = _read_state()
    now_ms = _now_ms()
    waits: List[float] = []
    keys = state.get("keys") or {}
    if not keys:
        return 0.0
    for entry in keys.values():
        until = float((entry or {}).get("cooldown_until_ms") or 0)
        waits.append(max(0.0, (until - now_ms) / 1000.0))
    return min(waits) if waits else 0.0


def record_rate_limit(
    profile_id: Optional[str] = None,
    retry_after_sec: Optional[float] = None,
    settings: Optional[Dict[str, Any]] = None,
    *,
    source: str = "rate_limit_429",
) -> float:
    """Mark a key in cooldown; return seconds until that key is ready."""
    cfg = QuotaConfig.from_settings(settings)
    profiles = [p for p, _ in _load_env_keys()]
    if not profiles:
        profiles = ["nvidia:default"]
    pid = profile_id or profiles[0]

    def _apply(state: Dict[str, Any]) -> float:
        keys = state.setdefault("keys", {})
        entry = keys.setdefault(pid, {})
        err_count = int(entry.get("error_count", 0)) + 1
        step_idx = min(err_count - 1, len(cfg.cooldown_steps_sec) - 1)
        cooldown_sec = float(retry_after_sec or cfg.cooldown_steps_sec[step_idx])
        until_ms = _now_ms() + cooldown_sec * 1000
        entry["error_count"] = err_count
        entry["cooldown_until_ms"] = until_ms
        entry["last_failure_ms"] = _now_ms()
        return cooldown_sec

    cooldown_sec = _mutate_state(_apply)
    _patch_auth_profile_cooldown(pid, _now_ms() + cooldown_sec * 1000)
    try:
        from app.llm.usage_monitor import record_request

        record_request(pid, source, is_rate_limit=True)
    except Exception:
        pass
    logger.warning(
        "LLM rate limit: profile=%s cooldown=%.0fs",
        pid,
        cooldown_sec,
    )
    return cooldown_sec


def record_success(profile_id: Optional[str] = None) -> None:
    profiles = [p for p, _ in _load_env_keys()]
    pid = profile_id or (profiles[0] if profiles else "nvidia:default")

    def _apply(state: Dict[str, Any]) -> None:
        keys = state.setdefault("keys", {})
        entry = keys.setdefault(pid, {})
        entry["last_used_ms"] = _now_ms()
        entry["error_count"] = 0
        entry["cooldown_until_ms"] = 0
        state.setdefault("global", {})["last_dispatch_ms"] = _now_ms()

    _mutate_state(_apply)


def _patch_auth_profile_cooldown(profile_id: str, until_ms: float) -> None:
    """Keep OpenClaw auth rotation aligned with our shorter cooldowns."""
    try:
        store = load_openclaw_auth_blob()
        stats = store.setdefault("usageStats", {})
        entry = stats.setdefault(profile_id, {})
        entry["cooldownUntil"] = int(until_ms)
        entry["lastFailureAt"] = int(_now_ms())
        save_openclaw_auth_blob(store)
    except Exception as exc:
        logger.debug("Could not patch auth-profiles cooldown: %s", exc)


def _patch_auth_last_good(profile_id: str) -> None:
    """Hint OpenClaw gateway toward the key RMP selected."""
    try:
        store = load_openclaw_auth_blob()
        store.setdefault("lastGood", {})["nvidia"] = profile_id
        save_openclaw_auth_blob(store)
    except Exception as exc:
        logger.debug("Could not patch auth-profiles lastGood: %s", exc)


def assign_openclaw_session_profile(session_key: str, profile_id: str) -> bool:
    """Pin an OpenClaw session to the RMP-selected NVIDIA profile.

    Only updates an existing session entry that already has a sessionId.
    Creating/touching a brand-new key before /hooks/agent races OpenClaw 2026.7+
    session lifecycle claims (CronSessionLifecycleClaimError).
    Callers must not pin nvidia:* onto openai/* sessions.
    """
    if not session_key or not profile_id:
        return False
    try:
        from app.openclaw_sessions import get_session_entry, patch_session_entry

        entry = get_session_entry(session_key)
        if not isinstance(entry, dict) or not entry.get("sessionId"):
            return False
        return patch_session_entry(
            session_key,
            {
                "authProfileOverride": profile_id,
                "authProfileOverrideSource": "user",
            },
        )
    except Exception as exc:
        logger.debug("Could not assign session profile %s: %s", session_key, exc)
        return False


def parse_retry_after(headers: Optional[Dict[str, str]], body: str = "") -> Optional[float]:
    if headers:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw:
            try:
                return float(raw.strip())
            except ValueError:
                pass
    match = re.search(r"retry[_ ]after[:\s]+(\d+)", body, re.I)
    if match:
        return float(match.group(1))
    return None


def is_gone_message(text: str) -> bool:
    """HTTP 410 / gone — skip the model; do not treat as a 429 cooldown."""
    blob = (text or "").lower()
    if re.search(r"\b410\b", blob):
        return True
    return "gone" in blob and "model" in blob


def is_rate_limit_message(text: str) -> bool:
    blob = (text or "").lower()
    if is_gone_message(blob):
        return False
    return "429" in blob or "rate limit" in blob or "too many requests" in blob


def _profile_ready_in_ms(
    state: Dict[str, Any],
    profile_id: str,
    now_ms: float,
    min_interval_sec: float,
) -> float:
    entry = state.get("keys", {}).get(profile_id, {})
    until = float(entry.get("cooldown_until_ms", 0) or 0)
    if until > now_ms:
        return until - now_ms
    last_used = float(entry.get("last_used_ms", 0) or 0)
    if last_used > 0:
        return max(0.0, (last_used + min_interval_sec * 1000) - now_ms)
    return 0.0


def classify_slot_kind(
    session_key: Optional[str] = None,
    *,
    tags: Optional[List[str]] = None,
    task_type: Optional[str] = None,
    kind: Optional[str] = None,
) -> str:
    """Stamp user, intake or canary/heartbeat from tags/task_type and the session key, not UUID substring."""
    if kind in ("user", "canary", "heartbeat", "intake"):
        return kind
    tag_set = {str(t).lower() for t in (tags or [])}
    tt = (task_type or "").lower()
    if tt == "canary" or "canary" in tag_set or "memory-canary" in tag_set:
        return "canary"
    if tt == "heartbeat" or "heartbeat" in tag_set:
        return "heartbeat"
    sk = (session_key or "").lower()
    if "heartbeat" in sk:
        return "heartbeat"
    if "canary" in sk:
        return "canary"
    if "rmp_intake_" in sk:
        return "intake"
    return "user"


def _pool_caps(max_concurrent: int) -> tuple[int, int]:
    """Split max_concurrent: 2 user + 1 canary when cap is 3."""
    cap = max(1, int(max_concurrent))
    if cap >= 3:
        return cap - 1, 1
    if cap == 2:
        return 1, 1
    return 1, 0


def _count_kind_slots(state: Dict[str, Any]) -> tuple[int, int]:
    user_n = 0
    canary_n = 0
    for slot in ((state.get("global") or {}).get("active_slots") or {}).values():
        k = _slot_kind(slot)
        if k in ("canary", "heartbeat"):
            canary_n += 1
        elif k != "intake":
            user_n += 1
    return user_n, canary_n


def _slot_is_canary(slot: Dict[str, Any]) -> bool:
    k = str(slot.get("kind") or "")
    if k in ("canary", "heartbeat"):
        return True
    if k == "user":
        return False
    sk = str(slot.get("session_key") or "")
    sk_lower = sk.lower()
    if "canary" in sk_lower or "heartbeat" in sk_lower:
        return True
    return False


def _preempt_canary_slot(state: Dict[str, Any]) -> Optional[str]:
    """Drop one canary/heartbeat slot so user intake/task work can reserve.

    Returns the canary task_id to cancel (if any), else None.
    """
    g = state.setdefault("global", {})
    slots = g.get("active_slots") or {}
    by_session = g.get("session_slots") or {}
    for slot_id, slot in list(slots.items()):
        if not _slot_is_canary(slot):
            continue
        sk = str(slot.get("session_key") or "")
        slots.pop(slot_id, None)
        if sk and by_session.get(sk) == slot_id:
            by_session.pop(sk, None)
        pid = str(slot.get("profile_id") or "")
        if pid:
            entry = state.setdefault("keys", {}).setdefault(pid, {})
            entry["in_flight"] = max(0, int(entry.get("in_flight", 0) or 0) - 1)
        logger.info("Preempted canary LLM slot session=%s", sk or slot_id)
        return _task_id_from_rmp_session(sk)
    return None


def _mutate_reserve(
    session_key: Optional[str],
    profiles: List[str],
    cfg: QuotaConfig,
    kind: str = "user",
    why: Optional[List[str]] = None,
) -> Optional[tuple[str, str]]:
    """Try one reservation. On failure, append the reason to ``why`` if given."""
    preempted: List[str] = []

    def _fail(reason: str) -> None:
        if why is not None:
            why.append(reason)
        return None

    def _apply(state: Dict[str, Any]) -> Optional[tuple[str, str]]:
        if session_key:
            existing = _existing_session_slot(state, session_key)
            if existing and existing[0]:
                return existing

        user_n, canary_n = _count_kind_slots(state)
        user_cap, canary_cap = _pool_caps(cfg.max_concurrent)
        total_cap = max(1, cfg.max_concurrent)

        if kind == "intake":
            intake_n = _count_intake_slots(state)
            if intake_n >= INTAKE_LANE_SLOTS:
                return _fail(f"intake lane busy ({intake_n}/{INTAKE_LANE_SLOTS})")
        elif kind in ("canary", "heartbeat"):
            if canary_n >= canary_cap or _active_slot_count(state) >= total_cap:
                return _fail(
                    f"canary slots full ({canary_n}/{canary_cap}, "
                    f"{_active_slot_count(state)}/{total_cap} total)"
                )
        else:
            if user_n >= user_cap or _active_slot_count(state) >= total_cap:
                tid = _preempt_canary_slot(state)
                if tid:
                    preempted.append(tid)
                user_n, canary_n = _count_kind_slots(state)
            if user_n >= user_cap or _active_slot_count(state) >= total_cap:
                return _fail(
                    f"user slots full ({user_n}/{user_cap}, "
                    f"{_active_slot_count(state)}/{total_cap} total)"
                )

        now_ms = _now_ms()
        pid = _pick_key(state, profiles, now_ms, rotation_mode=cfg.rotation_mode)
        if not pid:
            return _fail("every key is cooling down")
        ready_in_ms = _profile_ready_in_ms(state, pid, now_ms, cfg.min_interval_sec)
        if ready_in_ms > 0:
            return _fail(f"{pid} paced ({ready_in_ms / 1000:.1f}s left)")

        slot_id = str(uuid.uuid4())
        g = state.setdefault("global", {})
        g.setdefault("active_slots", {})[slot_id] = {
            "profile_id": pid,
            "started_ms": now_ms,
            "session_key": session_key or "",
            "kind": kind,
        }
        if session_key:
            g.setdefault("session_slots", {})[session_key] = slot_id

        entry = state.setdefault("keys", {}).setdefault(pid, {})
        entry["last_used_ms"] = now_ms
        entry["in_flight"] = int(entry.get("in_flight", 0) or 0) + 1
        entry["dispatch_count"] = int(entry.get("dispatch_count", 0) or 0) + 1
        g["last_dispatch_ms"] = now_ms
        g["last_profile_id"] = pid
        return pid, slot_id

    result = _mutate_state(_apply)
    for tid in preempted:
        try:
            from app.production.canary_sentinel import cancel_task_sync

            cancel_task_sync(tid, "user_preempt_canary")
        except Exception as exc:
            logger.warning("canary cancel after preempt failed: %s", exc)
    return result


def release_profile_sync(
    *,
    session_key: Optional[str] = None,
    slot_id: Optional[str] = None,
) -> bool:
    def _apply(state: Dict[str, Any]) -> bool:
        g = state.setdefault("global", {})
        by_session = g.get("session_slots") or {}
        slots = g.get("active_slots") or {}

        resolved_slot = slot_id
        if session_key and session_key in by_session:
            resolved_slot = by_session.pop(session_key)

        if not resolved_slot or resolved_slot not in slots:
            return False

        slot = slots.pop(resolved_slot)
        pid = str(slot.get("profile_id") or "")
        if pid:
            entry = state.setdefault("keys", {}).setdefault(pid, {})
            entry["in_flight"] = max(0, int(entry.get("in_flight", 0) or 0) - 1)
        return True

    return bool(_mutate_state(_apply))


def _task_id_from_rmp_session(session_key: str) -> Optional[str]:
    match = RMP_TASK_SESSION_RE.search(session_key or "")
    return match.group(1) if match else None


def _lookup_task_status_sync(task_id: str) -> Optional[str]:
    try:
        from sqlalchemy import create_engine, text

        from app.db.database import DATABASE_URL

        sync_url = DATABASE_URL.replace("+asyncpg", "+psycopg2")
        engine = create_engine(sync_url)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT status FROM tasks WHERE id = :id"),
                {"id": task_id},
            ).fetchone()
        return str(row[0]) if row else None
    except Exception as exc:
        logger.warning("Task status lookup failed for %s: %s", task_id, exc)
        return None


def reap_stale_llm_slots_sync(max_age_ms: int = DEFAULT_STALE_SLOT_MS) -> List[str]:
    """Release LLM slots held by terminal/missing tasks or old reservations."""
    state = _read_state()
    slots = dict((state.get("global") or {}).get("active_slots") or {})
    now_ms = _now_ms()
    actions: List[str] = []

    for slot_id, slot in slots.items():
        session_key = str(slot.get("session_key") or "")
        started_ms = float(slot.get("started_ms") or 0)
        task_id = _task_id_from_rmp_session(session_key)
        reason: Optional[str] = None
        age_limit = max_age_ms
        # Faster reap for canary/system sessions (they must not block user keys).
        sk_lower = session_key.lower()
        if (
            "canary" in sk_lower
            or "rmp_verify" in sk_lower
            or "heartbeat" in sk_lower
            or "rmp_intake_" in sk_lower
        ):
            age_limit = min(age_limit, CANARY_STALE_SLOT_MS)
        if task_id:
            # If the task goal/type looks like canary, also use short TTL.
            status = _lookup_task_status_sync(task_id)
            if status is None:
                reason = "missing_task"
            elif status in TERMINAL_TASK_STATUSES:
                reason = f"task_{status}"
            else:
                # Look up canary-ish goals cheaply via session key only above;
                # also shorten age for any rmp_task older than canary TTL when
                # the task row is a known canary type (best-effort).
                canaryish = _task_looks_like_canary_sync(task_id)
                if canaryish:
                    age_limit = min(age_limit, CANARY_STALE_SLOT_MS)

        if reason is None and started_ms and now_ms - started_ms > age_limit:
            reason = "stale_age"

        if reason:
            if release_profile_sync(session_key=session_key, slot_id=slot_id):
                actions.append(f"{session_key.split(':')[-1][:12]}:{reason}")

    if actions:
        logger.info("Reaped %d stale LLM slot(s): %s", len(actions), actions)
    return actions


def _task_looks_like_canary_sync(task_id: str) -> bool:
    try:
        from sqlalchemy import create_engine, text

        from app.db.database import DATABASE_URL

        sync_url = DATABASE_URL.replace("+asyncpg", "+psycopg2")
        engine = create_engine(sync_url)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT task_type, goal FROM tasks WHERE id = :id"),
                {"id": task_id},
            ).fetchone()
        if not row:
            return False
        task_type = (row[0] or "").lower()
        goal = (row[1] or "").upper()
        return task_type == "canary" or "RMP CANARY" in goal or "MEMORY CANARY" in goal
    except Exception:
        return False


def reserve_memory_lane_slot(
    priority: str,
    *,
    concurrency: int,
    per_minute: int,
    busy_enrich_slots: int,
    waiter_id: Optional[str] = None,
) -> Optional[str]:
    """One direct memory-model call slot, shared by every RMP process, or None to wait.

    Recall (a user is waiting) goes first: enrichment is not admitted while a recall
    waits, and holds at most ``busy_enrich_slots`` while user runs hold broker slots.
    """

    def _mutate(state: Dict[str, Any]) -> Optional[str]:
        lane = state.setdefault("memory_lane", {})
        slots = lane.setdefault("slots", {})
        waiting = lane.setdefault("recall_waiting", {})
        now = _now_ms()
        for sid in [s for s, v in slots.items() if now - float(v.get("started_ms") or 0) > MEMORY_LANE_STALE_MS]:
            slots.pop(sid, None)
        for wid in [w for w, ts in waiting.items() if now - float(ts or 0) > MEMORY_RECALL_WAIT_MS]:
            waiting.pop(wid, None)
        calls = [t for t in lane.get("calls_ms") or [] if now - float(t) < 60_000]
        lane["calls_ms"] = calls
        admit = len(slots) < max(1, concurrency) and len(calls) < max(1, per_minute)
        if admit and priority != "recall":
            enrich_active = sum(1 for v in slots.values() if v.get("priority") != "recall")
            user_busy = _count_kind_slots(state)[0] > 0
            admit = not waiting and not (user_busy and enrich_active >= max(1, busy_enrich_slots))
        if not admit:
            if priority == "recall" and waiter_id:
                waiting[waiter_id] = now
            return None
        if waiter_id:
            waiting.pop(waiter_id, None)
        slot_id = uuid.uuid4().hex
        slots[slot_id] = {"priority": priority, "started_ms": now, "pid": os.getpid()}
        calls.append(now)
        return slot_id

    return _mutate_state(_mutate)


def release_memory_lane_slot(slot_id: str, *, waiter_id: Optional[str] = None) -> None:
    def _mutate(state: Dict[str, Any]) -> None:
        lane = state.setdefault("memory_lane", {})
        (lane.setdefault("slots", {})).pop(slot_id, None)
        if waiter_id:
            (lane.setdefault("recall_waiting", {})).pop(waiter_id, None)

    _mutate_state(_mutate)


def _memory_lane_status(state: Dict[str, Any]) -> Dict[str, Any]:
    lane = state.get("memory_lane") or {}
    slots = lane.get("slots") or {}
    now = _now_ms()
    return {
        "active": len(slots),
        "recall_active": sum(1 for v in slots.values() if v.get("priority") == "recall"),
        "enrich_active": sum(1 for v in slots.values() if v.get("priority") != "recall"),
        "recall_waiting": len(lane.get("recall_waiting") or {}),
        "calls_last_minute": sum(1 for t in lane.get("calls_ms") or [] if now - float(t) < 60_000),
    }


def get_orchestration_status(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = QuotaConfig.from_settings(settings)
    state = _read_state()
    g = state.get("global") or {}
    slots = g.get("active_slots") or {}
    return {
        "memory_lane": _memory_lane_status(state),
        "max_concurrent": cfg.max_concurrent,
        "user_slots": _pool_caps(cfg.max_concurrent)[0],
        "canary_slots": _pool_caps(cfg.max_concurrent)[1],
        "active_slots": len(slots),
        "user_active": _count_kind_slots(state)[0],
        "canary_active": _count_kind_slots(state)[1],
        "intake_slots": INTAKE_LANE_SLOTS,
        "intake_active": _count_intake_slots(state),
        "rotation_mode": cfg.rotation_mode,
        "min_interval_sec": cfg.min_interval_sec,
        "sessions": {
            str(s.get("session_key") or sid): s.get("profile_id")
            for sid, s in slots.items()
        },
        "in_flight_by_profile": {
            pid: int((entry or {}).get("in_flight") or 0)
            for pid, entry in (state.get("keys") or {}).items()
        },
    }


async def reserve_profile(
    session_key: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    heartbeat=None,
    model: Optional[str] = None,
    tags: Optional[List[str]] = None,
    task_type: Optional[str] = None,
    kind: Optional[str] = None,
    deadline: Optional[float] = None,
) -> tuple[str, str]:
    """Reserve a concurrency slot and balanced NVIDIA profile for an agent run.

    ``deadline`` (epoch seconds) is the caller's own budget; the wait never outlives it.
    """
    cfg = QuotaConfig.from_settings(settings)
    profiles = [p for p, _ in _load_env_keys()] or ["nvidia:default"]
    slot_kind = classify_slot_kind(
        session_key, tags=tags, task_type=task_type, kind=kind
    )
    wait_cap = (
        min(float(cfg.max_wait_sec), CANARY_MAX_WAIT_SEC)
        if slot_kind in ("canary", "heartbeat")
        else cfg.max_wait_sec
    )
    started = time.time()
    give_up_at = started + wait_cap
    if deadline is not None:
        give_up_at = min(give_up_at, deadline)
    last_reap_ms = 0.0
    why: List[str] = []
    last_reason = "slow reservation attempt"

    while True:
        why.clear()
        # One attempt per lock hold: release_profile needs the same lock.
        async with _lock:
            result = _mutate_reserve(session_key, profiles, cfg, kind=slot_kind, why=why)
            if result:
                profile_id, slot_id = result
                if session_key:
                    from app.llm.model_policy import should_pin_nvidia_profile

                    if should_pin_nvidia_profile(model, profile_id):
                        assign_openclaw_session_profile(session_key, profile_id)
                _patch_auth_last_good(profile_id)
        if result:
            waited = time.time() - started
            if waited > RESERVE_WAIT_LOG_SEC:
                logger.info(
                    "LLM reserve waited %.1fs session=%s kind=%s last_reason=%s",
                    waited,
                    session_key or "-",
                    slot_kind,
                    last_reason,
                )
            logger.debug(
                "LLM reserve: profile=%s slot=%s session=%s kind=%s active=%s",
                profile_id,
                slot_id[:8],
                session_key or "-",
                slot_kind,
                _active_slot_count(_read_state()),
            )
            return profile_id, slot_id
        last_reason = why[-1] if why else "unknown"

        if slot_kind in ("canary", "heartbeat"):
            raise TimeoutError(
                "LLM quota: canary/heartbeat slot unavailable (user work has priority)"
            )

        remaining = give_up_at - time.time()
        if remaining <= 0:
            break

        now_ms = _now_ms()
        if now_ms - last_reap_ms > 15_000:
            last_reap_ms = now_ms
            try:
                reap_stale_llm_slots_sync(max_age_ms=10 * 60 * 1000)
            except Exception as exc:
                logger.debug("stale slot reap during reserve failed: %s", exc)

        state = _read_state()
        wait_ms = _soonest_ready_ms(
            state, profiles, _now_ms(), cfg.min_interval_sec
        )
        if _active_slot_count(state) >= cfg.max_concurrent:
            wait_ms = max(wait_ms, 1000.0)

        sleep_sec = min(max(wait_ms / 1000.0, 0.25), 30.0, remaining)
        if heartbeat:
            try:
                heartbeat()
            except Exception:
                pass
        await asyncio.sleep(sleep_sec)

    waited = time.time() - started
    logger.warning(
        "LLM reserve gave up after %.1fs session=%s kind=%s reason=%s",
        waited,
        session_key or "-",
        slot_kind,
        last_reason,
    )
    raise TimeoutError(
        f"LLM quota: no slot/profile available within {waited:.0f}s ({last_reason})"
    )


async def release_profile(
    *,
    session_key: Optional[str] = None,
    slot_id: Optional[str] = None,
) -> bool:
    async with _lock:
        released = release_profile_sync(session_key=session_key, slot_id=slot_id)
        if released:
            logger.debug(
                "LLM release: session=%s slot=%s active=%s",
                session_key or "-",
                (slot_id or "")[:8],
                _active_slot_count(_read_state()),
            )
        return released


def profile_for_session(session_key: str) -> Optional[str]:
    existing = _existing_session_slot(_read_state(), session_key)
    return existing[0] if existing else None


async def acquire(
    settings: Optional[Dict[str, Any]] = None,
    heartbeat=None,
) -> str:
    """Legacy helper — reserves a profile slot for the given session-less dispatch."""
    profile_id, _slot_id = await reserve_profile(
        session_key=None, settings=settings, heartbeat=heartbeat
    )
    return profile_id


def wait_for_dispatch_sync(
    settings: Optional[Dict[str, Any]] = None, *, deadline: Optional[float] = None
) -> str:
    """Blocking quota gate for sync embedders and other non-async callers.

    ``deadline`` (epoch seconds) caps the wait below ``max_wait_sec``.
    """
    cfg = QuotaConfig.from_settings(settings)
    profiles = [p for p, _ in _load_env_keys()]
    if not profiles:
        profiles = ["nvidia:default"]

    give_up_at = time.time() + cfg.max_wait_sec
    if deadline is not None:
        give_up_at = min(give_up_at, deadline)
    while time.time() < give_up_at:
        state = _read_state()
        now_ms = _now_ms()
        wait_ms = _soonest_ready_ms(state, profiles, now_ms, cfg.min_interval_sec)
        if wait_ms > 0:
            time.sleep(max(0.0, min(wait_ms / 1000.0, 30.0, give_up_at - time.time())))
            continue

        pid = _pick_key(state, profiles, now_ms, rotation_mode=cfg.rotation_mode)
        if pid:
            def _reserve(s: Dict[str, Any]) -> str:
                keys = s.setdefault("keys", {})
                entry = keys.setdefault(pid, {})
                entry["last_used_ms"] = now_ms
                entry["dispatch_count"] = int(entry.get("dispatch_count", 0) or 0) + 1
                s.setdefault("global", {})["last_dispatch_ms"] = now_ms
                s.setdefault("global", {})["last_profile_id"] = pid
                return pid

            chosen = _mutate_state(_reserve)
            _patch_auth_last_good(chosen)
            return chosen

        time.sleep(max(0.0, min(1.0, give_up_at - time.time())))

    raise TimeoutError(
        "LLM quota: no NVIDIA key available before the caller's deadline"
        if deadline is not None
        else f"LLM quota: no NVIDIA key available within {cfg.max_wait_sec:.0f}s"
    )


def sync_llm_auth_profiles() -> Dict[str, Any]:
    """Write NVIDIA + optional OpenAI keys into OpenClaw auth store (no secrets in return).

    OpenClaw 2026.9+ keeps credentials in shared SQLite (`authProfiles.store`).
    Writing leftover `auth-profiles.json` makes the gateway refuse NVIDIA with
    AUTH_PROFILE_MIGRATION_REQUIRED.
    """
    keys = _load_env_keys()
    openai_key = _read_env_value("OPENAI_API_KEY")
    if not keys and not openai_key:
        return {"synced": 0, "openai_synced": False, "profile_ids": []}

    store = load_openclaw_auth_blob()
    profiles = store.setdefault("profiles", {})
    last_good = store.setdefault("lastGood", {})
    for profile_id, api_key in keys:
        profiles[profile_id] = {
            "provider": "nvidia",
            "type": "api_key",
            "key": api_key,
        }
    if keys:
        last_good["nvidia"] = keys[0][0]
    openai_synced = False
    if openai_key:
        profiles["openai:default"] = {
            "provider": "openai",
            "type": "api_key",
            "key": openai_key,
        }
        last_good["openai"] = "openai:default"
        openai_synced = True
    save_openclaw_auth_blob(store)

    profile_ids = [p for p, _ in keys]
    if openai_synced:
        profile_ids.append("openai:default")
    logger.info(
        "Synced %d NVIDIA auth profile(s)%s: %s",
        len(keys),
        " + openai:default" if openai_synced else "",
        [p for p, _ in keys],
    )
    return {
        "synced": len(keys),
        "openai_synced": openai_synced,
        "profile_ids": profile_ids,
    }


def sync_nvidia_auth_profiles() -> Dict[str, Any]:
    """Backward-compatible name — systemd ExecStartPre still calls this."""
    return sync_llm_auth_profiles()
