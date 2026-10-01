import json
import os
import time
from pathlib import Path

import pytest

from app.llm import quota_broker as qb


def test_an_unreadable_env_file_reads_as_no_keys(monkeypatch):
    """Coding units hide /etc/openclaw; Path.is_file() raises PermissionError on a hidden path."""
    class Hidden:
        def read_text(self, encoding=None):
            raise PermissionError(13, "Permission denied", "/etc/openclaw/openclaw.env")

    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", Hidden())
    for name in [n for n in os.environ if n.startswith("NVIDIA_API_KEY")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("NVIDIA_API_KEY_2", "nvapi-from-env")
    assert [pid for pid, _ in qb._load_env_keys()] == ["nvidia:key2"]
    assert qb._read_env_value("MISSING_NAME") == ""


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
    monkeypatch.setattr(
        "app.production.canary_sentinel.cancel_task_sync", lambda *_a, **_k: True
    )

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
                            "kind": "canary",
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


def test_canary_cannot_take_both_user_slots(monkeypatch, tmp_path):
    state_file = tmp_path / "llm_quota.json"
    env_file = tmp_path / "openclaw.env"
    env_file.write_text("NVIDIA_API_KEY=k1\n")
    monkeypatch.setattr(qb, "STATE_PATH", state_file)
    monkeypatch.setattr(qb, "LOCK_PATH", tmp_path / ".llm_quota.lock")
    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    monkeypatch.setattr(qb, "_today_usage_by_profile", lambda: {})
    monkeypatch.setattr(qb, "_task_looks_like_canary_sync", lambda _tid: False)
    state_file.write_text(json.dumps({"keys": {}, "global": {"active_slots": {}, "session_slots": {}}}))
    cfg = qb.QuotaConfig(max_concurrent=3, min_interval_sec=0.0)
    a = qb._mutate_reserve("agent:main:rmp_task_canary_one", ["nvidia:default"], cfg, kind="canary")
    b = qb._mutate_reserve("agent:main:rmp_task_canary_two", ["nvidia:default"], cfg, kind="canary")
    assert a is not None
    assert b is None
    state = json.loads(state_file.read_text())
    kinds = [s.get("kind") for s in state["global"]["active_slots"].values()]
    assert kinds.count("canary") == 1


@pytest.mark.asyncio
async def test_canary_reserve_does_not_wait_1800s(monkeypatch, tmp_path):
    state_file = tmp_path / "llm_quota.json"
    env_file = tmp_path / "openclaw.env"
    env_file.write_text("NVIDIA_API_KEY=k1\n")
    monkeypatch.setattr(qb, "STATE_PATH", state_file)
    monkeypatch.setattr(qb, "LOCK_PATH", tmp_path / ".llm_quota.lock")
    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    monkeypatch.setattr(qb, "_today_usage_by_profile", lambda: {})
    monkeypatch.setattr(
        qb,
        "assign_openclaw_session_profile",
        lambda session_key, profile_id: None,
    )
    now_ms = time.time() * 1000
    state_file.write_text(
        json.dumps(
            {
                "keys": {"nvidia:default": {"last_used_ms": 0, "in_flight": 1}},
                "global": {
                    "active_slots": {
                        "slot-c": {
                            "profile_id": "nvidia:default",
                            "started_ms": now_ms,
                            "session_key": "agent:main:rmp_task_canary_held",
                            "kind": "canary",
                        }
                    },
                    "session_slots": {"agent:main:rmp_task_canary_held": "slot-c"},
                },
            }
        )
    )
    settings = {"llm_quota": {"max_concurrent": 3, "min_interval_sec": 0, "max_wait_sec": 1800}}
    t0 = time.time()
    with pytest.raises(TimeoutError, match="canary"):
        await qb.reserve_profile(
            session_key="agent:main:rmp_task_canary_second",
            settings=settings,
            tags=["canary"],
            task_type="canary",
        )
    assert time.time() - t0 < 5


def test_user_reserve_cancels_canary_task(monkeypatch, tmp_path):
    state_file = tmp_path / "llm_quota.json"
    env_file = tmp_path / "openclaw.env"
    env_file.write_text("NVIDIA_API_KEY=k1\n")
    monkeypatch.setattr(qb, "STATE_PATH", state_file)
    monkeypatch.setattr(qb, "LOCK_PATH", tmp_path / ".llm_quota.lock")
    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    monkeypatch.setattr(qb, "_today_usage_by_profile", lambda: {})
    cancelled = []

    def fake_cancel(tid, reason="x"):
        cancelled.append((tid, reason))
        return True

    monkeypatch.setattr(
        "app.production.canary_sentinel.cancel_task_sync", fake_cancel
    )
    canary_tid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    now_ms = time.time() * 1000
    state_file.write_text(
        json.dumps(
            {
                "keys": {"nvidia:default": {"last_used_ms": 0, "in_flight": 1}},
                "global": {
                    "active_slots": {
                        "slot-c": {
                            "profile_id": "nvidia:default",
                            "started_ms": now_ms,
                            "session_key": f"agent:main:rmp_task_{canary_tid}",
                            "kind": "canary",
                        }
                    },
                    "session_slots": {
                        f"agent:main:rmp_task_{canary_tid}": "slot-c"
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
        kind="user",
    )
    assert result is not None
    assert cancelled == [(canary_tid, "user_preempt_canary")]
