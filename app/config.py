"""Central RMP configuration loaded from openclaw.json and settings.json."""
import json
import os
import secrets
from functools import lru_cache
from typing import Any, Dict, Optional

# Host layout defaults match the production VPS. Override in CI / tests via env:
#   OPENCLAW_HOME, RMP_ROOT, RMP_SETTINGS_PATH, …
OPENCLAW_HOME = os.environ.get("OPENCLAW_HOME", "/root/.openclaw")
RMP_ROOT = os.environ.get("RMP_ROOT", os.path.join(OPENCLAW_HOME, "rmp"))
OPENCLAW_CONFIG_PATH = os.environ.get(
    "OPENCLAW_CONFIG_PATH", os.path.join(OPENCLAW_HOME, "openclaw.json")
)
SETTINGS_PATH = os.environ.get(
    "RMP_SETTINGS_PATH", os.path.join(RMP_ROOT, "settings.json")
)
SESSIONS_JSON_PATH = os.environ.get(
    "OPENCLAW_SESSIONS_JSON",
    os.path.join(OPENCLAW_HOME, "agents", "main", "sessions", "sessions.json"),
)
AUTH_PROFILES_PATH = os.environ.get(
    "OPENCLAW_AUTH_PROFILES",
    os.path.join(OPENCLAW_HOME, "agents", "main", "agent", "auth-profiles.json"),
)
RMP_DATA_DIR = os.environ.get("RMP_DATA_DIR", os.path.join(RMP_ROOT, "data"))


def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data: Any) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


DEFAULT_VECTOR_MEMORY = {
    "enabled": True,
    "qdrant_mode": "server",
    "qdrant_host": "127.0.0.1",
    "qdrant_port": 6333,
    "qdrant_path": os.path.join(RMP_DATA_DIR, "qdrant"),
    "collection_name": "rmp_memories",
    "embedder_provider": "nvidia",
    "embedder_model": "nvidia/nv-embed-v1",
    "embedding_dims": 4096,
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
    "max_concurrent": 3,
    "cooldown_steps_sec": [15, 30, 60, 120],
}

DEFAULT_TASK_REGISTRY = {
    "enabled": True,
    "collection_name": "rmp_task_registry",
    "intake_mode": "enforce",  # off | shadow | enforce
    "similarity_threshold": 0.72,
    "intake_confidence_threshold": 65,
    "intake_cache_sec": 60,
    "intake_llm_timeout_sec": 60,
    "intake_model": "nvidia/deepseek-ai/deepseek-v4-flash-0731",
    "intake_model_fallbacks": [],
    "intake_vector_deadline_sec": 15,
    "qdrant_query_timeout_sec": 8,
    "backfill_days": 90,
    "rework_max_attempts": 3,
    "temporal_half_life_days": 30,
    "recurrence_intervals": {
        "heartbeat": 25,
        "health_canary": 55,
        "memory_canary": 360,
        "cron_default": 55,
    },
}


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
    return [primary] + fallbacks if primary else fallbacks


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


def load_settings() -> dict:
    settings = _read_json(SETTINGS_PATH, {})
    env_key = os.environ.get("RMP_API_KEY", "").strip()
    if env_key:
        settings["api_key"] = env_key
    elif not settings.get("api_key"):
        settings["api_key"] = secrets.token_hex(32)
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
    _write_json(SETTINGS_PATH, settings)
    return settings


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


def save_settings(settings: dict) -> None:
    _write_json(SETTINGS_PATH, settings)


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
