"""ops/sync_nvidia_keys.py, the key sync rmp-api, rmp-worker and the gateway run before each start.

The script is loaded as its units run it; its env file and OpenClaw's SQLite auth store are in tmp_path.
"""
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from app.llm import quota_broker as qb

ROOT = Path(__file__).resolve().parents[1]


def load():
    module_spec = importlib.util.spec_from_file_location("sync_nvidia_keys", ROOT / "ops" / "sync_nvidia_keys.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


@pytest.fixture
def store(monkeypatch, tmp_path):
    """OpenClaw 2026.9+'s auth store, empty, and an env file the test fills; the profiles the sync left, by id."""
    db = tmp_path / "openclaw.sqlite"
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

    env_file = tmp_path / "openclaw.env"
    monkeypatch.setattr(qb, "OPENCLAW_ENV_PATH", env_file)
    monkeypatch.setattr(qb, "OPENCLAW_STATE_DB", db)
    monkeypatch.setattr(qb, "AUTH_PROFILES_PATH", tmp_path / "auth-profiles.json")
    monkeypatch.setattr(qb, "USE_SQLITE_AUTH", True)
    for name in list(os.environ):
        if name.startswith("NVIDIA_API_KEY") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name, raising=False)
    # The script puts its checkout first on sys.path; the test's own path comes back afterwards.
    monkeypatch.setattr(sys, "path", list(sys.path))

    def profiles():
        return sorted(qb.load_openclaw_auth_blob()["profiles"])

    return type("Store", (), {"env": env_file, "profiles": staticmethod(profiles)})


def test_an_openai_key_alone_is_a_successful_sync(store):
    store.env.write_text("OPENAI_API_KEY=sk-test-openai\n")
    assert load().main() == 0
    assert store.profiles() == ["openai:default"]


def test_no_key_at_all_fails_the_sync(store):
    store.env.write_text("")
    assert load().main() == 1
    assert store.profiles() == []


def test_the_script_finds_app_through_its_own_checkout(store):
    """The gateway unit sets no PYTHONPATH: the script's own checkout, never a literal production path, finds app."""
    load()
    assert sys.path[0] == str(ROOT)
