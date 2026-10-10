"""Hermetic host paths for every test, set before any test imports app.

app.config resolves its paths at import time, so this runs at import, in order: the session guard; the refusal of
an inherited path variable that points at production; one temp root, ROOT, for everything the suite creates outside
a test's own tmp_path; and a default under ROOT for each path variable the environment lacks, plus the test
database. The refusal comes before ROOT exists: pytest runs no unconfigure after a conftest fails to import, so a
root made first would be left behind.
"""
import os
import tempfile
from pathlib import Path

import pytest

from tests import production_guard

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = production_guard.session_guard(REPO_ROOT)
_production = production_guard.production_values(os.environ, GUARD)
if _production:
    raise pytest.UsageError("; ".join(
        f"{name}={value} points at production; unset it or point it at a temporary directory"
        for name, value in _production))
ROOT = Path(tempfile.mkdtemp(prefix="rmp-tests-"))
os.environ.update(production_guard.suite_environment(os.environ, ROOT, REPO_ROOT))
for sub in ("openclaw/agents/main/agent", "openclaw/agents/main/sessions", "openclaw/workspace", "openclaw/cron",
            "data", "aura-code"):
    (ROOT / sub).mkdir(parents=True)


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
