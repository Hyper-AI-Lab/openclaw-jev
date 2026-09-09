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
    assert not qb.is_rate_limit_message("HTTP 410 Gone: model retired")
    assert qb.is_gone_message("HTTP 410 Gone: model retired")


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
    monkeypatch.setattr(qb, "USE_SQLITE_AUTH", False)
    # Host may export NVIDIA_API_KEY_3+; isolate the test to the stub env file only.
    for name in list(os.environ):
        if name.startswith("NVIDIA_API_KEY") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name, raising=False)

    result = qb.sync_nvidia_auth_profiles()
    assert result["synced"] == 2
    assert result["openai_synced"] is False
    store = json.loads(auth_file.read_text())
    assert store["profiles"]["nvidia:default"]["key"] == "alpha"
    assert store["profiles"]["nvidia:key2"]["key"] == "beta"
    assert store["lastGood"]["nvidia"] == "nvidia:default"


def test_sync_nvidia_auth_profiles_sqlite(monkeypatch, tmp_path):
    env_file = tmp_path / "openclaw.env"
    env_file.write_text("NVIDIA_API_KEY=alpha\nNVIDIA_API_KEY_2=beta\n")
    db = tmp_path / "openclaw.sqlite"
    leftover = tmp_path / "auth-profiles.json"
    leftover.write_text(json.dumps({"version": 1, "profiles": {"stale": {"provider": "x"}}}))
    import sqlite3

    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE config_machine_state ("
        "state_key TEXT NOT NULL PRIMARY KEY, value_json TEXT NOT NULL, updated_at_ms INTEGER NOT NULL)"
    )
    con.execute(
        "INSERT INTO config_machine_state VALUES (?,?,?)",
        ("authProfiles.store", json.dumps({"version": 1, "profiles": {"openai:default": {"provider": "openai"}}}), 1),
    )
    con.execute(
        "INSERT INTO config_machine_state VALUES (?,?,?)",
        ("authProfiles.state", json.dumps({"version": 1, "lastGood": {}, "usageStats": {}}), 1),
    )
    con.commit()
    con.close()

    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    monkeypatch.setattr(qb, "OPENCLAW_STATE_DB", db)
    monkeypatch.setattr(qb, "AUTH_PROFILES_PATH", leftover)
    monkeypatch.setattr(qb, "USE_SQLITE_AUTH", True)
    for name in list(os.environ):
        if name.startswith("NVIDIA_API_KEY") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name, raising=False)

    result = qb.sync_nvidia_auth_profiles()
    assert result["synced"] == 2
    assert result["openai_synced"] is False
    assert not leftover.is_file()
    blob = qb.load_openclaw_auth_blob()
    assert blob["profiles"]["nvidia:default"]["key"] == "alpha"
    assert blob["profiles"]["openai:default"]["provider"] == "openai"
    assert list(leftover.parent.glob("auth-profiles.json.retired-*"))


def test_sync_openai_and_nvidia_sqlite(monkeypatch, tmp_path):
    env_file = tmp_path / "openclaw.env"
    env_file.write_text(
        "NVIDIA_API_KEY=alpha\nNVIDIA_API_KEY_2=beta\nOPENAI_API_KEY=sk-test-openai\n"
    )
    db = tmp_path / "openclaw.sqlite"
    leftover = tmp_path / "auth-profiles.json"
    leftover.write_text(json.dumps({"version": 1, "profiles": {"stale": {}}}))
    import sqlite3

    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE config_machine_state ("
        "state_key TEXT NOT NULL PRIMARY KEY, value_json TEXT NOT NULL, updated_at_ms INTEGER NOT NULL)"
    )
    con.execute(
        "INSERT INTO config_machine_state VALUES (?,?,?)",
        ("authProfiles.store", json.dumps({"version": 1, "profiles": {}}), 1),
    )
    con.execute(
        "INSERT INTO config_machine_state VALUES (?,?,?)",
        ("authProfiles.state", json.dumps({"version": 1, "lastGood": {}, "usageStats": {}}), 1),
    )
    con.commit()
    con.close()

    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    monkeypatch.setattr(qb, "OPENCLAW_STATE_DB", db)
    monkeypatch.setattr(qb, "AUTH_PROFILES_PATH", leftover)
    monkeypatch.setattr(qb, "USE_SQLITE_AUTH", True)
    for name in list(os.environ):
        if name.startswith("NVIDIA_API_KEY") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name, raising=False)

    result = qb.sync_llm_auth_profiles()
    assert result["synced"] == 2
    assert result["openai_synced"] is True
    assert "openai:default" in result["profile_ids"]
    assert "sk-test-openai" not in json.dumps(result)
    assert not leftover.is_file()
    blob = qb.load_openclaw_auth_blob()
    assert blob["profiles"]["openai:default"]["key"] == "sk-test-openai"
    assert blob["lastGood"]["openai"] == "openai:default"
    assert blob["profiles"]["nvidia:default"]["key"] == "alpha"
    dumped = json.dumps(result)
    assert "alpha" not in dumped
    assert list(leftover.parent.glob("auth-profiles.json.retired-*"))


def test_user_intake_preempts_canary_slot(monkeypatch, tmp_path):
    state_file = tmp_path / "llm_quota.json"
    env_file = tmp_path / "openclaw.env"
    env_file.write_text("NVIDIA_API_KEY=k1\n")
    monkeypatch.setattr(qb, "STATE_PATH", state_file)
    monkeypatch.setattr(qb, "LOCK_PATH", tmp_path / ".llm_quota.lock")
    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    monkeypatch.setattr(qb, "_today_usage_by_profile", lambda: {})
    monkeypatch.setattr(qb, "_task_looks_like_canary_sync", lambda _tid: False)

    now_ms = time.time() * 1000
    state_file.write_text(
        json.dumps(
            {
                "keys": {
                    "nvidia:default": {
                        "last_used_ms": 0,
                        "in_flight": 1,
                        "dispatch_count": 1,
                    }
                },
                "global": {
                    "active_slots": {
                        "slot-canary": {
                            "profile_id": "nvidia:default",
                            "started_ms": now_ms,
                            "session_key": "agent:main:rmp_task_canary_health",
                        }
                    },
                    "session_slots": {
                        "agent:main:rmp_task_canary_health": "slot-canary"
                    },
                },
            }
        )
    )
    cfg = qb.QuotaConfig(max_concurrent=1, min_interval_sec=0.0)
    result = qb._mutate_reserve(
        "agent:main:rmp_intake_user_abcd1234",
        ["nvidia:default"],
        cfg,
    )
    assert result is not None
    profile_id, slot_id = result
    assert profile_id == "nvidia:default"
    assert slot_id != "slot-canary"
    state = json.loads(state_file.read_text())
    slots = state["global"]["active_slots"]
    assert "slot-canary" not in slots
    assert any(
        s.get("session_key") == "agent:main:rmp_intake_user_abcd1234"
        for s in slots.values()
    )


def test_user_does_not_preempt_other_user_slot(monkeypatch, tmp_path):
    state_file = tmp_path / "llm_quota.json"
    env_file = tmp_path / "openclaw.env"
    env_file.write_text("NVIDIA_API_KEY=k1\n")
    monkeypatch.setattr(qb, "STATE_PATH", state_file)
    monkeypatch.setattr(qb, "LOCK_PATH", tmp_path / ".llm_quota.lock")
    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    monkeypatch.setattr(qb, "_today_usage_by_profile", lambda: {})
    monkeypatch.setattr(qb, "_task_looks_like_canary_sync", lambda _tid: False)

    now_ms = time.time() * 1000
    state_file.write_text(
        json.dumps(
            {
                "keys": {
                    "nvidia:default": {
                        "last_used_ms": 0,
                        "in_flight": 1,
                        "dispatch_count": 1,
                    }
                },
                "global": {
                    "active_slots": {
                        "slot-user": {
                            "profile_id": "nvidia:default",
                            "started_ms": now_ms,
                            "session_key": "agent:main:rmp_task_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        }
                    },
                    "session_slots": {
                        "agent:main:rmp_task_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": "slot-user"
                    },
                },
            }
        )
    )
    cfg = qb.QuotaConfig(max_concurrent=1, min_interval_sec=0.0)
    result = qb._mutate_reserve(
        "agent:main:rmp_intake_other_ffff9999",
        ["nvidia:default"],
        cfg,
    )
    assert result is None
    state = json.loads(state_file.read_text())
    assert "slot-user" in state["global"]["active_slots"]
