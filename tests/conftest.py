"""Hermetic host paths for every test, as CI sets them.

app.config resolves these at import time, so they are set before any test imports
app. A local run must never read or write the live settings.json or OpenClaw home.
"""
import os
import tempfile
from pathlib import Path

import pytest

if "RMP_SETTINGS_PATH" not in os.environ:
    _root = Path(tempfile.mkdtemp(prefix="rmp-tests-"))
    os.environ["OPENCLAW_HOME"] = str(_root / "openclaw")
    os.environ["RMP_ROOT"] = str(Path(__file__).resolve().parents[1])
    os.environ["RMP_DATA_DIR"] = str(_root / "data")
    os.environ["RMP_SETTINGS_PATH"] = str(_root / "settings.json")
    for sub in ("agents/main/agent", "agents/main/sessions", "workspace", "cron"):
        (_root / "openclaw" / sub).mkdir(parents=True, exist_ok=True)
    (_root / "data").mkdir(parents=True, exist_ok=True)

# The app's default URL, and the one /etc/rmp/rmp.env exports, is the live database on this host.
os.environ["DATABASE_URL"] = (
    f"sqlite+aiosqlite:///{Path(tempfile.mkdtemp(prefix='rmp-tests-db-')) / 'unmocked.db'}"
)


@pytest.fixture(autouse=True)
def _no_live_deep_index(monkeypatch):
    """The deep-memory index would reach this host's Qdrant and embed with its key; tests stub it."""
    if os.environ.get("RMP_QDRANT_IT") == "1":
        return
    from app.deep_memory import index

    def off_limits():
        raise RuntimeError("hermetic tests: the deep-memory index is off limits")

    monkeypatch.setattr(index, "_client", off_limits)
    monkeypatch.setattr(index, "_read_openai_key", lambda: "")


@pytest.fixture(autouse=True)
def _no_live_claude_sessions(monkeypatch, tmp_path_factory):
    """Aura's live Claude sessions: a test that ends or prunes them stops real turns, as one did from a suite
    Claude ran inside a session. Every test gets an empty directory of its own."""
    from app.coding import direct

    root = tmp_path_factory.mktemp("claude-direct")
    monkeypatch.setattr(direct, "DIRECT_DIR", root / "direct")
    monkeypatch.setattr(direct, "CONFIG_DIR", root / "claude-config")


@pytest.fixture(autouse=True)
def _no_live_gateway_abort(monkeypatch):
    """Stopping a task calls the openclaw CLI against this host's gateway; tests stub it."""
    from app import openclaw_control

    async def refused(session_key):
        return {"key": session_key, "status": "error", "error": "hermetic tests: no gateway"}

    def no_helper(self):
        raise RuntimeError("hermetic tests: no gateway helper")

    monkeypatch.setattr(openclaw_control, "abort_session", refused)
    monkeypatch.setattr(openclaw_control.GatewayHelper, "_command", no_helper)
