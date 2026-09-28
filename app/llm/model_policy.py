"""Canonical RMP LLM model policy.

Secrets stay in `/etc/openclaw/openclaw.env`. This module owns model ids,
OpenClaw config shape, and who may pin a NVIDIA auth profile on a session.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

PRIMARY_MODEL = "openai/gpt-5-nano"
# NVIDIA-hosted, so it uses NVIDIA auth and key rotation despite the "openai/" model id.
FALLBACK_MODELS = ("nvidia/openai/gpt-oss-20b",)
# Hook-created sessions without a model (the Process Evaluator) run on the subagent model.
SUBAGENT_MODEL = PRIMARY_MODEL
GLM_MODEL = "nvidia/z-ai/glm-5.2"
# NVIDIA ended MiniMax M3 on 2026-09-09 and DeepSeek V4 Flash on 2026-09-21 (HTTP 410).
RETIRED_MODEL_MARKERS = ("glm", "deepseek", "minimax")
OPENAI_MODEL_ID = "gpt-5-nano"
OPENAI_PROVIDER_BASE_URL = "https://api.openai.com/v1"
OPENAI_AUTH_PROFILE = "openai:default"
OPENCLAW_AGENT_RUNTIME = {"id": "openclaw"}
# "0m" makes OpenClaw keep its heartbeat monitor job disabled. RMP canaries own liveness.
HEARTBEAT_EVERY = "0m"

_OPENAI_MODEL_ROW = {
    "id": OPENAI_MODEL_ID,
    "name": "GPT-5 nano",
    "reasoning": True,
    "input": ["text"],
    "cost": {
        "input": 0.05,
        "output": 0.4,
        "cacheRead": 0.005,
        "cacheWrite": 0,
    },
    "contextWindow": 400000,
    "maxTokens": 128000,
    "api": "openai-completions",
}

_NVIDIA_FALLBACK_ROW = {
    "id": FALLBACK_MODELS[0].removeprefix("nvidia/"),
    "name": "GPT-OSS 20B (NVIDIA NIM)",
    "reasoning": True,
    "input": ["text"],
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    "contextWindow": 128000,
    "maxTokens": 8192,
    "api": "openai-completions",
}

_MODEL_ALIASES = {
    PRIMARY_MODEL: {
        "alias": "GPT-5 nano (OpenAI primary)",
        "agentRuntime": dict(OPENCLAW_AGENT_RUNTIME),
        # thinking=medium first-token often exceeds the 5s idle patch.
        "params": {"thinking": "low"},
    },
    FALLBACK_MODELS[0]: {"alias": "GPT-OSS 20B (NVIDIA fallback)"},
}


def allowed_models() -> List[str]:
    return [PRIMARY_MODEL, *FALLBACK_MODELS]


def intake_model_chain() -> List[str]:
    return allowed_models()


def model_uses_nvidia_auth(model: Optional[str] = None) -> bool:
    ref = (model or PRIMARY_MODEL).strip()
    return ref.startswith("nvidia/")


def should_pin_nvidia_profile(model: Optional[str], profile_id: str) -> bool:
    """Pin nvidia:keyN only onto nvidia/* sessions — never onto openai/gpt-5-nano."""
    if not str(profile_id or "").startswith("nvidia:"):
        return False
    return model_uses_nvidia_auth(model)


def openai_key_present() -> bool:
    from app.llm.quota_broker import _read_env_value

    return bool(_read_env_value("OPENAI_API_KEY"))


def drop_unwired_openai(models: List[str]) -> List[str]:
    """Skip openai/* refs until OPENAI_API_KEY is set (NVIDIA fallbacks still run)."""
    kept = [m for m in models if str(m).strip()]
    if openai_key_present():
        return kept
    return [m for m in kept if not m.startswith("openai/")]


def _is_retired_ref(value: str) -> bool:
    return any(marker in (value or "").lower() for marker in RETIRED_MODEL_MARKERS)


def _ensure_model_row(provider_cfg: Dict[str, Any], canonical: Dict[str, Any]) -> bool:
    models = provider_cfg.setdefault("models", [])
    if not isinstance(models, list):
        provider_cfg["models"] = [dict(canonical)]
        return True
    for idx, row in enumerate(models):
        if isinstance(row, dict) and str(row.get("id") or "") == canonical["id"]:
            if row != canonical:
                models[idx] = dict(canonical)
                return True
            return False
    models.insert(0, dict(canonical))
    return True


def _ensure_openai_model_row(openai_cfg: Dict[str, Any]) -> bool:
    return _ensure_model_row(openai_cfg, _OPENAI_MODEL_ROW)


def apply_openclaw_policy(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Write OpenClaw primary, fallbacks, allowlist, openai row, auth.order, and heartbeat.

    Does not write API keys into openclaw.json.
    """
    if config_path is None:
        from app.config import OPENCLAW_CONFIG_PATH

        config_path = Path(OPENCLAW_CONFIG_PATH)
    path = Path(config_path)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    changed: List[str] = []

    agents = cfg.setdefault("agents", {})
    defaults = agents.setdefault("defaults", {})
    model = defaults.setdefault("model", {})
    if model.get("primary") != PRIMARY_MODEL:
        model["primary"] = PRIMARY_MODEL
        changed.append("agents.defaults.model.primary")
    want_fb = list(FALLBACK_MODELS)
    if model.get("fallbacks") != want_fb:
        model["fallbacks"] = want_fb
        changed.append("agents.defaults.model.fallbacks")
    if defaults.get("thinkingDefault") != "low":
        defaults["thinkingDefault"] = "low"
        changed.append("agents.defaults.thinkingDefault")

    aliases = defaults.setdefault("models", {})
    if not isinstance(aliases, dict):
        aliases = {}
        defaults["models"] = aliases
    for ref, meta in _MODEL_ALIASES.items():
        if aliases.get(ref) != meta:
            aliases[ref] = dict(meta)
            changed.append(f"agents.defaults.models:{ref}")
    for dead in [k for k in list(aliases) if _is_retired_ref(str(k))]:
        aliases.pop(dead, None)
        changed.append(f"drop-alias:{dead}")

    subagents = defaults.setdefault("subagents", {})
    if subagents.get("model") != SUBAGENT_MODEL:
        subagents["model"] = SUBAGENT_MODEL
        changed.append("agents.defaults.subagents.model")

    heartbeat = defaults.setdefault("heartbeat", {})
    if heartbeat.get("every") != HEARTBEAT_EVERY:
        heartbeat["every"] = HEARTBEAT_EVERY
        changed.append("agents.defaults.heartbeat.every")

    policy = defaults.setdefault("modelPolicy", {})
    allow = allowed_models()
    if policy.get("allow") != allow:
        policy["allow"] = allow
        changed.append("agents.defaults.modelPolicy.allow")

    providers = cfg.setdefault("models", {}).setdefault("providers", {})
    openai = providers.setdefault("openai", {})
    if openai.get("baseUrl") != OPENAI_PROVIDER_BASE_URL:
        openai["baseUrl"] = OPENAI_PROVIDER_BASE_URL
        changed.append("models.providers.openai.baseUrl")
    if openai.get("api") != "openai-completions":
        openai["api"] = "openai-completions"
        changed.append("models.providers.openai.api")
    # Agent auth is SQLite openai:default. ${OPENAI_API_KEY} on the provider
    # marks the whole plugin cold if systemd has not reloaded EnvironmentFile.
    if "apiKey" in openai:
        openai.pop("apiKey", None)
        changed.append("models.providers.openai.drop-apiKey-interp")
    if _ensure_openai_model_row(openai):
        changed.append("models.providers.openai.models")

    nvidia = providers.get("nvidia")
    if isinstance(nvidia, dict):
        nmodels = nvidia.get("models") or []
        if isinstance(nmodels, list):
            filtered = [
                row
                for row in nmodels
                if not (
                    isinstance(row, dict) and _is_retired_ref(str(row.get("id") or ""))
                )
            ]
            if len(filtered) != len(nmodels):
                nvidia["models"] = filtered
                changed.append("models.providers.nvidia.drop-retired")
        if _ensure_model_row(nvidia, _NVIDIA_FALLBACK_ROW):
            changed.append("models.providers.nvidia.models:fallback")

    auth = cfg.setdefault("auth", {})
    profiles = auth.setdefault("profiles", {})
    want_profile = {"provider": "openai", "mode": "api_key"}
    if profiles.get(OPENAI_AUTH_PROFILE) != want_profile:
        profiles[OPENAI_AUTH_PROFILE] = want_profile
        changed.append("auth.profiles.openai:default")
    order = auth.setdefault("order", {})
    if order.get("openai") != [OPENAI_AUTH_PROFILE]:
        order["openai"] = [OPENAI_AUTH_PROFILE]
        changed.append("auth.order.openai")

    plugins = cfg.setdefault("plugins", {})
    allow_pl = list(plugins.get("allow") or [])
    if "openai" not in allow_pl:
        if "nvidia" in allow_pl:
            allow_pl.insert(allow_pl.index("nvidia") + 1, "openai")
        else:
            allow_pl.append("openai")
        plugins["allow"] = allow_pl
        changed.append("plugins.allow")

    # Adding the openai chat provider makes OpenClaw default memory.search to
    # OpenAI embeddings. RMP owns vectors; disable builtin search until a key
    # exists rather than aborting sync / mixing indexes.
    memory = cfg.setdefault("memory", {})
    search = memory.setdefault("search", {})
    if search.get("enabled") is not False:
        search["enabled"] = False
        changed.append("memory.search.enabled")

    if changed:
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return {
        "changed": changed,
        "primary": PRIMARY_MODEL,
        "fallbacks": list(FALLBACK_MODELS),
        "subagent_model": SUBAGENT_MODEL,
        "path": str(path),
    }
