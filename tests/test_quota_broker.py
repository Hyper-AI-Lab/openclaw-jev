import json
import os
import time
from pathlib import Path

import pytest

from app.llm import quota_broker as qb


def test_is_rate_limit_message():
    assert qb.is_rate_limit_message("429 status code (no body)")
    assert qb.is_rate_limit_message("Rate limit exceeded")
    assert not qb.is_rate_limit_message("ok")


def test_cooldown_steps_are_short(monkeypatch, tmp_path):
    state_file = tmp_path / "llm_quota.json"
    auth_file = tmp_path / "auth-profiles.json"
    env_file = tmp_path / "openclaw.env"
    env_file.write_text("NVIDIA_API_KEY=test-key-1\nNVIDIA_API_KEY_2=test-key-2\n")
    auth_file.write_text(json.dumps({"version": 1, "profiles": {}, "usageStats": {}}))

    monkeypatch.setattr(qb, "STATE_PATH", state_file)
    monkeypatch.setattr(qb, "AUTH_PROFILES_PATH", auth_file)
    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)

    wait1 = qb.record_rate_limit("nvidia:default", settings={"llm_quota": {}})
    assert wait1 == 15
    wait2 = qb.record_rate_limit("nvidia:default", settings={"llm_quota": {}})
    assert wait2 == 30

    state = json.loads(state_file.read_text())
    until = state["keys"]["nvidia:default"]["cooldown_until_ms"]
    assert until > time.time() * 1000


def test_pick_key_skips_cooling_profile(monkeypatch, tmp_path):
    state_file = tmp_path / "llm_quota.json"
    env_file = tmp_path / "openclaw.env"
    env_file.write_text("NVIDIA_API_KEY=k1\nNVIDIA_API_KEY_2=k2\n")
    monkeypatch.setattr(qb, "STATE_PATH", state_file)
    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    monkeypatch.setattr(qb, "_today_usage_by_profile", lambda: {})

    now_ms = time.time() * 1000
    state = {
        "keys": {
            "nvidia:default": {"cooldown_until_ms": now_ms + 60000},
            "nvidia:key2": {"cooldown_until_ms": 0, "last_used_ms": 0},
        },
        "global": {"last_dispatch_ms": 0},
    }
    picked = qb._pick_key(state, ["nvidia:default", "nvidia:key2"], now_ms)
    assert picked == "nvidia:key2"


def test_pick_key_balanced_prefers_lower_usage(monkeypatch, tmp_path):
    env_file = tmp_path / "openclaw.env"
    env_file.write_text("NVIDIA_API_KEY=k1\nNVIDIA_API_KEY_2=k2\n")
    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    monkeypatch.setattr(
        qb,
        "_today_usage_by_profile",
        lambda: {
            "nvidia:default": {
                "requests": 100,
                "total_tokens": 500_000,
                "input_tokens": 0,
                "output_tokens": 0,
                "rate_limits": 0,
            },
            "nvidia:key2": {
                "requests": 5,
                "total_tokens": 10_000,
                "input_tokens": 0,
                "output_tokens": 0,
                "rate_limits": 0,
            },
        },
    )
    now_ms = time.time() * 1000
    state = {
        "keys": {
            "nvidia:default": {"cooldown_until_ms": 0, "last_used_ms": 0},
            "nvidia:key2": {"cooldown_until_ms": 0, "last_used_ms": 0},
        },
        "global": {},
    }
    picked = qb._pick_key(
        state, ["nvidia:default", "nvidia:key2"], now_ms, rotation_mode="balanced"
    )
    assert picked == "nvidia:key2"


def test_sync_nvidia_auth_profiles(monkeypatch, tmp_path):
    auth_file = tmp_path / "auth-profiles.json"
    env_file = tmp_path / "openclaw.env"
    env_file.write_text("NVIDIA_API_KEY=alpha\nNVIDIA_API_KEY_2=beta\n")
    monkeypatch.setattr(qb, "AUTH_PROFILES_PATH", auth_file)
    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    # Host may export NVIDIA_API_KEY_3+; isolate the test to the stub env file only.
    for name in list(os.environ):
        if name.startswith("NVIDIA_API_KEY"):
            monkeypatch.delenv(name, raising=False)

    result = qb.sync_nvidia_auth_profiles()
    assert result["synced"] == 2
    store = json.loads(auth_file.read_text())
    assert store["profiles"]["nvidia:default"]["key"] == "alpha"
    assert store["profiles"]["nvidia:key2"]["key"] == "beta"
    assert store["lastGood"]["nvidia"] == "nvidia:default"
