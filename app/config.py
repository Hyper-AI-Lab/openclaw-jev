"""Central RMP configuration loaded from openclaw.json and settings.json."""
import fcntl
import json
import os
import secrets
import tempfile
import time
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Callable, Dict, Iterator, Optional

from app.llm.model_policy import FALLBACK_MODELS, PRIMARY_MODEL

# Host layout defaults match the production VPS. Override in CI / tests via env:
#   OPENCLAW_HOME, RMP_ROOT, RMP_SETTINGS_PATH, …


def _openclaw_home() -> str:
    return os.environ.get("OPENCLAW_HOME", "/root/.openclaw")


def _rmp_root() -> str:
    return os.environ.get("RMP_ROOT", os.path.join(_openclaw_home(), "rmp"))


def _rmp_data_dir() -> str:
    return os.environ.get("RMP_DATA_DIR", os.path.join(_rmp_root(), "data"))


def _settings_path() -> str:
    return os.environ.get("RMP_SETTINGS_PATH", os.path.join(_rmp_root(), "settings.json"))


def _openclaw_config_path() -> str:
    return os.environ.get(
        "OPENCLAW_CONFIG_PATH", os.path.join(_openclaw_home(), "openclaw.json")
    )


def _sessions_json_path() -> str:
    return os.environ.get(
        "OPENCLAW_SESSIONS_JSON",
        os.path.join(_openclaw_home(), "agents", "main", "sessions", "sessions.json"),
    )


def _auth_profiles_path() -> str:
    return os.environ.get(
        "OPENCLAW_AUTH_PROFILES",
        os.path.join(_openclaw_home(), "agents", "main", "agent", "auth-profiles.json"),
    )


OPENCLAW_HOME = _openclaw_home()
RMP_ROOT = _rmp_root()
OPENCLAW_CONFIG_PATH = _openclaw_config_path()
SETTINGS_PATH = _settings_path()
SESSIONS_JSON_PATH = _sessions_json_path()
AUTH_PROFILES_PATH = _auth_profiles_path()
RMP_DATA_DIR = _rmp_data_dir()

# PEP 562: resolve path attrs even when a long-lived process imported an older
# config module that lacked a newly-added name (from app.config import X).
_PATH_ATTR_RESOLVERS = {
    "OPENCLAW_HOME": _openclaw_home,
    "RMP_ROOT": _rmp_root,
    "RMP_DATA_DIR": _rmp_data_dir,
    "SETTINGS_PATH": _settings_path,
    "OPENCLAW_CONFIG_PATH": _openclaw_config_path,
    "SESSIONS_JSON_PATH": _sessions_json_path,
    "AUTH_PROFILES_PATH": _auth_profiles_path,
}


def __getattr__(name: str) -> Any:
    resolver = _PATH_ATTR_RESOLVERS.get(name)
    if resolver is not None:
        return resolver()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


class SettingsCorruptError(RuntimeError):
    """settings.json exists but is not a JSON object. Never read as empty settings."""


def _read_settings_file() -> dict:
    for attempt in range(5):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except ValueError:
            # Only a writer that bypasses update_settings can expose a half-written file.
            time.sleep(0.02 * (attempt + 1))
            continue
        if isinstance(data, dict):
            return data
        break
    raise SettingsCorruptError(f"{SETTINGS_PATH} is not a valid JSON object")


@contextmanager
def _settings_write_lock() -> Iterator[None]:
    os.makedirs(os.path.dirname(SETTINGS_PATH) or ".", exist_ok=True)
    with open(f"{SETTINGS_PATH}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _write_settings_file(data: dict) -> None:
    """Readers see the old or the new file, never a partial one."""
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(SETTINGS_PATH) or ".", prefix=".settings.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(SETTINGS_PATH):
            os.chmod(tmp, os.stat(SETTINGS_PATH).st_mode & 0o777)
        os.replace(tmp, SETTINGS_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


DEFAULT_VECTOR_MEMORY = {
    "enabled": True,
    "qdrant_mode": "server",
    "qdrant_host": "127.0.0.1",
    "qdrant_port": 6333,
    "qdrant_path": os.path.join(RMP_DATA_DIR, "qdrant"),
    "collection_name": "rmp_memories_openai_3small",
    "embedder_provider": "openai",
    "embedder_model": "text-embedding-3-small",
    "embedding_dims": 1536,
    "semantic_recall_limit": 5,
}


DEFAULT_TELEMETRY = {
    "enabled": True,
    "service_name": "rmp",
    "otlp_endpoint": "",
    "console_export": False,
}


DEFAULT_ARTIFACT_STORE = {
    "enabled": True,
    "root_path": os.path.join(RMP_DATA_DIR, "artifacts"),
}


DEFAULT_PRODUCTION = {
    "alerting": {
        "enabled": False,
        "webhook_url": "",
    },
    "scanner_auto_restart": False,
    "scanner_managed_ids": [],
    # Aura owner Slack user id (OpenClaw 2026.7 DM origins use slack:channel:U…)
    "slack_owner_user_id": "U0AELFYTLKS",
}

DEFAULT_LLM_QUOTA = {
    "provider": "nvidia",
    "min_interval_sec": 5.0,
    "max_wait_sec": 1800.0,
    "max_concurrent": 4,
    "cooldown_steps_sec": [15, 30, 60, 120],
}

DEFAULT_TASK_REGISTRY = {
    "enabled": True,
    "collection_name": "rmp_task_registry_openai_3small",
    "intake_mode": "enforce",  # off | shadow | enforce
    "similarity_threshold": 0.72,
    "intake_confidence_threshold": 65,
    "intake_cache_sec": 60,
    "intake_llm_timeout_sec": 40,
    "intake_model": PRIMARY_MODEL,
    "intake_model_fallbacks": list(FALLBACK_MODELS),
    "intake_vector_deadline_sec": 10,
    "qdrant_query_timeout_sec": 8,
    "backfill_days": 90,
    "rework_max_attempts": 20,
    "strategy_change_attempt": 10,
    "escalate_user_attempt": 20,
    "temporal_half_life_days": 30,
    "recurrence_intervals": {
        "heartbeat": 25,
        "health_canary": 55,
        "memory_canary": 360,
        "cron_default": 55,
    },
}


DEFAULT_DEEP_MEMORY = {
    # Ingestion, the hybrid index and the fast context.
    "enabled": True,
    # The IA's deep recall during user tasks, and the judged follow-up it can trigger.
    "recall_enabled": False,
    "followups_enabled": False,
    "collection_name": "rmp_deep_memory_v1",
    "embedder_model": "text-embedding-3-large",
    "embedding_dims": 1536,
    # Direct model calls (app/llm/openai_direct.py).
    "lane_concurrency": 3,
    "lane_requests_per_minute": 60,
    "lane_busy_enrich_slots": 1,
    "llm_daily_token_budget": 4_000_000,
    "llm_call_deadline_sec": 120,
    # Ingestion.
    "ingest_concurrency": 2,
    "tool_document_min_chars": 2000,
    # Aura's fast context, and the deep recall that may follow up on her reply.
    "fast_context_deadline_sec": 3.0,
    "fast_context_max_chars": 6000,
    # Dense similarity a fact needs to reach Aura's fast context (text-embedding-3-large, 1536 dims).
    "fast_context_fact_floor": 0.30,
    "recall_deadline_sec": 180,
    "followup_wait_sec": 300,
}


def get_deep_memory_config() -> dict:
    return dict(load_settings().get("deep_memory") or DEFAULT_DEEP_MEMORY)


DEFAULT_CODING = {
    # Coding tasks: Claude Code as the aura-coder user, run and verified by RMP.
    "enabled": True,
    "claude_version": "2.1.280",
    "model": "opus",
    "fallback_model": "sonnet",
    "max_turns": 200,
    "max_rounds": 3,
    "run_timeout_sec": 5400,
    "memory_max": "3G",
    "cpu_quota": "250%",
    "tasks_max": 1024,
    # This host's services on loopback; aura-coder may not connect to them.
    "blocked_tcp_ports": [22, "4317-4318", 5432, 6006, "6333-6334", "6933-6939", "7233-7243",
                          8000, 8791, 9222, "18789-18899"],
    "verify_timeout_sec": 1800,
    "job_retention_days": 14,
    "diff_limit_chars": 200_000,
    # The repositories coding tasks may change. "self" deploys to this host after Kirill's approval;
    # "pr" pushes a branch and opens a pull request. {venv} is the shared read-only test venv.
    "repositories": {
        "rmp": {"remote": "Hyper-AI-Lab/openclaw-jev", "source": "/root/.openclaw/rmp", "branch": "main",
                "deploy": "self", "setup": [],
                "tests": [["{venv}/bin/python", "-m", "pytest", "-q", "-p", "no:warnings"],
                          ["node", "--test", "tests/node/*.test.js"]]},
        "agentic-design": {"remote": "Hyper-AI-Lab/agentic-design", "branch": "main", "deploy": "pr",
                           "setup": [["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]],
                           "tests": [["npm", "test"]]},
        "cursor-dual-agent-loop": {"remote": "Hyper-AI-Lab/cursor-dual-agent-loop", "branch": "main", "deploy": "pr",
                                          "setup": [["python3", "-m", "venv", ".aura/venv"],
                                                    [".aura/venv/bin/pip", "install", "-q", "cursor-sdk", "pyyaml", "pytest"]],
                                          "tests": [[".aura/venv/bin/python", "-m", "pytest", "-q", "tests/"]]},
        "cyber-ai-team": {"remote": "Hyper-AI-Lab/cyber-ai-team", "branch": "main", "deploy": "pr",
                                 "setup": [["python3", "-m", "venv", ".aura/venv"],
                                           [".aura/venv/bin/pip", "install", "-q", "-r", "backend/requirements.txt", "pytest",
                                            "pytest-asyncio", "greenlet"]],
                                 "tests": [["sh", "-c", "cd backend && PYTHONPATH=src ../.aura/venv/bin/python -m pytest -q"]]},
    },
}


def get_coding_config() -> dict:
    return dict(load_settings().get("coding") or DEFAULT_CODING)


def get_intake_models() -> list[str]:
    """Ordered intake LLM models: primary then fallbacks (OpenClaw provider/model refs)."""
    cfg = get_task_registry_config()
    primary = str(
        cfg.get("intake_model")
        or DEFAULT_TASK_REGISTRY["intake_model"]
    ).strip()
    raw_fb = cfg.get("intake_model_fallbacks")
    if raw_fb is None:
        raw_fb = DEFAULT_TASK_REGISTRY["intake_model_fallbacks"]
    fallbacks = [
        str(m).strip()
        for m in (raw_fb if isinstance(raw_fb, list) else [])
        if str(m).strip() and str(m).strip() != primary
    ]
    from app.llm.model_policy import drop_unwired_openai

    chain = drop_unwired_openai([primary] + fallbacks if primary else fallbacks)
    # OpenClaw already walks the same fallbacks. When OpenAI is unwired, one
    # NVIDIA fallback turn beats two sequential 5s-idle budgets.
    if chain and not any(m.startswith("openai/") for m in chain):
        return chain[:1]
    return chain


def get_primary_agent_model() -> str:
    """OpenClaw agents.defaults.model.primary — the policy primary with NVIDIA fallbacks."""
    cfg = _read_json(OPENCLAW_CONFIG_PATH, {})
    model = (cfg.get("agents") or {}).get("defaults", {}).get("model") or {}
    if isinstance(model, dict):
        primary = str(model.get("primary") or "").strip()
        if primary:
            from app.llm.model_policy import drop_unwired_openai

            wired = drop_unwired_openai([primary, *FALLBACK_MODELS])
            return wired[0] if wired else FALLBACK_MODELS[0]
    if isinstance(model, str) and model.strip():
        from app.llm.model_policy import drop_unwired_openai

        wired = drop_unwired_openai([model.strip(), *FALLBACK_MODELS])
        return wired[0] if wired else FALLBACK_MODELS[0]
    from app.llm.model_policy import drop_unwired_openai

    wired = drop_unwired_openai([PRIMARY_MODEL, *FALLBACK_MODELS])
    return wired[0] if wired else PRIMARY_MODEL


def get_intake_timeout_budget() -> dict:
    """Aligned intake timeouts — workflow, activity, and OpenClaw poll."""
    from app.task_registry.intake_timeouts import intake_timeout_budget

    cfg = get_task_registry_config()
    llm_sec = int(cfg.get("intake_llm_timeout_sec", 60))
    context_sec = int(cfg.get("intake_vector_deadline_sec", 15))
    return intake_timeout_budget(llm_sec, context_sec)


def get_api_key() -> str:
    """API key from environment (preferred) or settings.json."""
    env_key = os.environ.get("RMP_API_KEY", "").strip()
    if env_key:
        return env_key
    return load_settings().get("api_key", "")


def _ensure_api_key() -> str:
    with _settings_write_lock():
        raw = _read_settings_file()
        if not raw.get("api_key"):
            raw["api_key"] = secrets.token_hex(32)
            _write_settings_file(raw)
        return raw["api_key"]


def load_settings() -> dict:
    """File settings merged with defaults. Reading never rewrites the file."""
    settings = _read_settings_file()
    env_key = os.environ.get("RMP_API_KEY", "").strip()
    if env_key:
        settings["api_key"] = env_key
    elif not settings.get("api_key"):
        settings["api_key"] = _ensure_api_key()
    settings.setdefault("intermediate_updates", True)
    settings.setdefault("development_mode", False)
    settings.setdefault("suspend_slack_notifications", False)
    settings.setdefault("suspend_task_interception", False)
    vm = settings.get("vector_memory") or {}
    merged_vm = {**DEFAULT_VECTOR_MEMORY, **vm}
    settings["vector_memory"] = merged_vm
    tel = settings.get("telemetry") or {}
    settings["telemetry"] = {**DEFAULT_TELEMETRY, **tel}
    art = settings.get("artifact_store") or {}
    settings["artifact_store"] = {**DEFAULT_ARTIFACT_STORE, **art}
    prod = settings.get("production") or {}
    merged_prod = {**DEFAULT_PRODUCTION, **prod}
    if "alerting" in prod:
        merged_prod["alerting"] = {
            **DEFAULT_PRODUCTION["alerting"],
            **prod.get("alerting", {}),
        }
    settings["production"] = merged_prod
    llm_q = settings.get("llm_quota") or {}
    settings["llm_quota"] = {**DEFAULT_LLM_QUOTA, **llm_q}
    tr = settings.get("task_registry") or {}
    settings["task_registry"] = {**DEFAULT_TASK_REGISTRY, **tr}
    dm = settings.get("deep_memory") or {}
    settings["deep_memory"] = {**DEFAULT_DEEP_MEMORY, **dm}
    coding = settings.get("coding") or {}
    settings["coding"] = {**DEFAULT_CODING, **coding}
    return settings


def update_settings(mutate: Callable[[dict], None]) -> dict:
    """The only settings writer: locked read-modify-write of the file's own keys.

    ``mutate`` edits the stored settings in place (defaults are not merged in).
    Returns the merged view after the write.
    """
    with _settings_write_lock():
        raw = _read_settings_file()
        mutate(raw)
        _write_settings_file(raw)
    return load_settings()


def is_development_mode() -> bool:
    return bool(load_settings().get("development_mode"))


def should_suspend_slack() -> bool:
    s = load_settings()
    return bool(s.get("development_mode") and s.get("suspend_slack_notifications"))


def should_suspend_interception() -> bool:
    s = load_settings()
    return bool(s.get("development_mode") and s.get("suspend_task_interception"))


def should_send_intermediate_updates() -> bool:
    s = load_settings()
    if s.get("development_mode"):
        return False
    return bool(s.get("intermediate_updates", False))


def get_vector_memory_config() -> dict:
    return load_settings().get("vector_memory", DEFAULT_VECTOR_MEMORY)


def is_vector_memory_enabled() -> bool:
    return bool(get_vector_memory_config().get("enabled", True))


def get_llm_quota_config() -> dict:
    return load_settings().get("llm_quota", DEFAULT_LLM_QUOTA)


def get_telemetry_config() -> dict:
    return load_settings().get("telemetry", DEFAULT_TELEMETRY)


def is_telemetry_enabled() -> bool:
    return bool(get_telemetry_config().get("enabled", False))


def get_artifact_store_config() -> dict:
    return load_settings().get("artifact_store", DEFAULT_ARTIFACT_STORE)


def get_task_registry_config() -> dict:
    return load_settings().get("task_registry", DEFAULT_TASK_REGISTRY)


def get_task_registry_intake_mode() -> str:
    mode = (get_task_registry_config().get("intake_mode") or "enforce").strip().lower()
    if mode not in ("off", "shadow", "enforce"):
        return "enforce"
    return mode


def is_task_registry_enabled() -> bool:
    return bool(get_task_registry_config().get("enabled", True))


def is_artifact_store_enabled() -> bool:
    return bool(get_artifact_store_config().get("enabled", True))


@lru_cache(maxsize=1)
def get_openclaw_hook_token() -> str:
    cfg = _read_json(OPENCLAW_CONFIG_PATH, {})
    hooks = cfg.get("hooks", {})
    token = hooks.get("token", "")
    if token.startswith("hook-token-"):
        return token
    gateway_token = cfg.get("gateway", {}).get("auth", {}).get("token", "")
    if gateway_token:
        return f"hook-token-{gateway_token}"
    return token or ""


def get_openclaw_url() -> str:
    cfg = _read_json(OPENCLAW_CONFIG_PATH, {})
    port = cfg.get("gateway", {}).get("port", 18789)
    return f"http://127.0.0.1:{port}"


def get_slack_bot_token() -> str:
    cfg = _read_json(OPENCLAW_CONFIG_PATH, {})
    return cfg.get("channels", {}).get("slack", {}).get("botToken", "")


def get_slack_owner_user_id() -> str:
    """Configured owner DM user id used when session origin cannot be parsed."""
    prod = load_settings().get("production") or {}
    uid = str(prod.get("slack_owner_user_id") or "").strip()
    return uid if uid.startswith("U") else ""


def get_main_slack_session_key() -> str:
    return "agent:main:main"
