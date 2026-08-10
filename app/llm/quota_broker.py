"""Proactive LLM dispatch gate: pacing, multi-key cooldowns, short backoff."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger("rmp.llm_quota")

AUTH_PROFILES_PATH = Path("/root/.openclaw/agents/main/agent/auth-profiles.json")
STATE_PATH = Path("/root/.openclaw/rmp/data/llm_quota.json")
LOCK_PATH = STATE_PATH.parent / ".llm_quota.lock"
OPENCLAW_ENV_PATH = Path("/etc/openclaw/openclaw.env")

# Shorter than OpenClaw defaults (1m/5m/25m/1h) — prefer rotating keys.
COOLDOWN_STEPS_SEC = (15, 30, 60, 120)
DEFAULT_MIN_INTERVAL_SEC = 5.0
DEFAULT_MAX_WAIT_SEC = 1800.0
DEFAULT_MAX_CONCURRENT = 3

RMP_TASK_SESSION_RE = re.compile(
    r"rmp_task_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "compensated", "stopped_by_user", "cancelled"}
)
DEFAULT_STALE_SLOT_MS = 90 * 60 * 1000

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


def _active_slot_count(state: Dict[str, Any]) -> int:
    return len((state.get("global") or {}).get("active_slots") or {})


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
    if not AUTH_PROFILES_PATH.is_file():
        return
    try:
        store = json.loads(AUTH_PROFILES_PATH.read_text(encoding="utf-8"))
        stats = store.setdefault("usageStats", {})
        entry = stats.setdefault(profile_id, {})
        entry["cooldownUntil"] = int(until_ms)
        entry["lastFailureAt"] = int(_now_ms())
        _atomic_write_json(AUTH_PROFILES_PATH, store)
    except Exception as exc:
        logger.debug("Could not patch auth-profiles cooldown: %s", exc)


def _patch_auth_last_good(profile_id: str) -> None:
    """Hint OpenClaw gateway toward the key RMP selected."""
    if not AUTH_PROFILES_PATH.is_file():
        return
    try:
        store = json.loads(AUTH_PROFILES_PATH.read_text(encoding="utf-8"))
        store.setdefault("lastGood", {})["nvidia"] = profile_id
        _atomic_write_json(AUTH_PROFILES_PATH, store)
    except Exception as exc:
        logger.debug("Could not patch auth-profiles lastGood: %s", exc)


def assign_openclaw_session_profile(session_key: str, profile_id: str) -> bool:
    """Pin an OpenClaw session to the RMP-selected NVIDIA profile.

    Only updates an existing session entry that already has a sessionId.
    Creating/touching a brand-new key before /hooks/agent races OpenClaw 2026.7+
    session lifecycle claims (CronSessionLifecycleClaimError).
    """
    sessions_path = Path("/root/.openclaw/agents/main/sessions/sessions.json")
    if not session_key or not profile_id or not sessions_path.is_file():
        return False
    try:
        store = json.loads(sessions_path.read_text(encoding="utf-8"))
        entry = store.get(session_key)
        if not isinstance(entry, dict) or not entry.get("sessionId"):
            return False
        entry["authProfileOverride"] = profile_id
        entry["authProfileOverrideSource"] = "user"
        store[session_key] = entry
        _atomic_write_json(sessions_path, store)
        return True
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


def is_rate_limit_message(text: str) -> bool:
    blob = (text or "").lower()
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


def _mutate_reserve(
    session_key: Optional[str],
    profiles: List[str],
    cfg: QuotaConfig,
) -> Optional[tuple[str, str]]:
    def _apply(state: Dict[str, Any]) -> Optional[tuple[str, str]]:
        if session_key:
            existing = _existing_session_slot(state, session_key)
            if existing and existing[0]:
                return existing

        if _active_slot_count(state) >= max(1, cfg.max_concurrent):
            return None

        now_ms = _now_ms()
        pid = _pick_key(state, profiles, now_ms, rotation_mode=cfg.rotation_mode)
        if not pid:
            return None
        if _profile_ready_in_ms(state, pid, now_ms, cfg.min_interval_sec) > 0:
            return None

        slot_id = str(uuid.uuid4())
        g = state.setdefault("global", {})
        g.setdefault("active_slots", {})[slot_id] = {
            "profile_id": pid,
            "started_ms": now_ms,
            "session_key": session_key or "",
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

    return _mutate_state(_apply)


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

        if started_ms and now_ms - started_ms > max_age_ms:
            reason = "stale_age"
        elif task_id:
            status = _lookup_task_status_sync(task_id)
            if status is None:
                reason = "missing_task"
            elif status in TERMINAL_TASK_STATUSES:
                reason = f"task_{status}"

        if reason:
            if release_profile_sync(session_key=session_key, slot_id=slot_id):
                actions.append(f"{session_key.split(':')[-1][:12]}:{reason}")

    if actions:
        logger.info("Reaped %d stale LLM slot(s): %s", len(actions), actions)
    return actions


def get_orchestration_status(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = QuotaConfig.from_settings(settings)
    state = _read_state()
    g = state.get("global") or {}
    slots = g.get("active_slots") or {}
    return {
        "max_concurrent": cfg.max_concurrent,
        "active_slots": len(slots),
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
) -> tuple[str, str]:
    """Reserve a concurrency slot and balanced NVIDIA profile for an agent run."""
    cfg = QuotaConfig.from_settings(settings)
    profiles = [p for p, _ in _load_env_keys()] or ["nvidia:default"]
    deadline = time.time() + cfg.max_wait_sec
    last_reap_ms = 0.0

    async with _lock:
        while time.time() < deadline:
            result = _mutate_reserve(session_key, profiles, cfg)
            if result:
                profile_id, slot_id = result
                if session_key:
                    assign_openclaw_session_profile(session_key, profile_id)
                _patch_auth_last_good(profile_id)
                logger.debug(
                    "LLM reserve: profile=%s slot=%s session=%s active=%s",
                    profile_id,
                    slot_id[:8],
                    session_key or "-",
                    _active_slot_count(_read_state()),
                )
                return profile_id, slot_id

            # Opportunistically free slots held by dead/old work so intake isn't starved.
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

            sleep_sec = min(max(wait_ms / 1000.0, 0.25), 30.0)
            if heartbeat:
                try:
                    heartbeat()
                except Exception:
                    pass
            await asyncio.sleep(sleep_sec)

    raise TimeoutError(
        f"LLM quota: no slot/profile available within {cfg.max_wait_sec:.0f}s"
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


def wait_for_dispatch_sync(settings: Optional[Dict[str, Any]] = None) -> str:
    """Blocking quota gate for sync embedders and other non-async callers."""
    cfg = QuotaConfig.from_settings(settings)
    profiles = [p for p, _ in _load_env_keys()]
    if not profiles:
        profiles = ["nvidia:default"]

    deadline = time.time() + cfg.max_wait_sec
    while time.time() < deadline:
        state = _read_state()
        now_ms = _now_ms()
        wait_ms = _soonest_ready_ms(state, profiles, now_ms, cfg.min_interval_sec)
        if wait_ms > 0:
            time.sleep(min(wait_ms / 1000.0, 30.0))
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

        time.sleep(1.0)

    raise TimeoutError(
        f"LLM quota: no NVIDIA key available within {cfg.max_wait_sec:.0f}s"
    )


def sync_nvidia_auth_profiles() -> Dict[str, Any]:
    """Write multi-key NVIDIA profiles to auth-profiles.json (no secrets in return)."""
    keys = _load_env_keys()
    if not keys:
        return {"synced": 0, "profile_ids": []}

    store: Dict[str, Any] = {"version": 1, "profiles": {}, "lastGood": {}, "usageStats": {}}
    if AUTH_PROFILES_PATH.is_file():
        try:
            store = json.loads(AUTH_PROFILES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    profiles = store.setdefault("profiles", {})
    last_good = store.setdefault("lastGood", {})
    for profile_id, api_key in keys:
        profiles[profile_id] = {
            "provider": "nvidia",
            "type": "api_key",
            "key": api_key,
        }
    last_good["nvidia"] = keys[0][0]

    AUTH_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(AUTH_PROFILES_PATH, store)

    profile_ids = [p for p, _ in keys]
    logger.info("Synced %d NVIDIA auth profile(s): %s", len(profile_ids), profile_ids)
    return {"synced": len(profile_ids), "profile_ids": profile_ids}
