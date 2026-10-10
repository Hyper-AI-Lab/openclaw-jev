"""Hermetic host paths for every test, set before any test imports app, and the production guard.

app.config resolves its paths at import time, so this runs at import, in order: the session guard; the refusal of
an inherited path variable that points at production; one temp root, ROOT, for everything the suite creates outside
a test's own tmp_path; a default under ROOT for each path variable the environment lacks, plus the test database;
and the guard's audit hook. The refusal comes before ROOT exists: pytest runs no unconfigure after a conftest fails to
import, so a root made first would be left behind. pytest's basetemp is in ROOT unless --basetemp names a directory
to keep, and ROOT is removed when pytest unconfigures, or at exit when pytest stopped before configuring.
"""
import atexit
import os
import shutil
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
_CONFIGURED = False


@atexit.register
def _remove_the_root_of_an_unconfigured_run():
    """pytest can stop after importing this conftest and before configuring: a usage error, or --version. No
    unconfigure runs then, so ROOT goes here. No test has run on that path; after a configured run this does
    nothing, and a writer a test left running still shows as a leftover."""
    if not _CONFIGURED:
        shutil.rmtree(ROOT)


os.environ.update(production_guard.suite_environment(os.environ, ROOT, REPO_ROOT))
for sub in ("openclaw/agents/main/agent", "openclaw/agents/main/sessions", "openclaw/workspace", "openclaw/cron",
            "data", "aura-code"):
    (ROOT / sub).mkdir(parents=True)
# npm (ops/openclaw_preflight.py) turns on Node's compile cache, which leaves <tmp>/node-compile-cache behind.
os.environ.setdefault("NODE_DISABLE_COMPILE_CACHE", "1")
production_guard.install(GUARD)
# Violations recorded outside any test: at collection, or in a module or session fixture.
_OUTSIDE_TESTS = []


@pytest.fixture(autouse=True)
def _production_guard():
    """A test that touched a production path, this host's services or its service tools fails, even when its code
    swallowed the refusal. Set up before every fixture the test requests, so their teardowns count too."""
    _OUTSIDE_TESTS.extend(GUARD.take())
    yield
    violations = GUARD.take()
    if violations:
        pytest.fail("hermetic tests: this test reached production\n" + production_guard.report(violations),
                    pytrace=False)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Before the tmpdir plugin reads it: pytest's temp dirs go in ROOT unless --basetemp names a directory."""
    global _CONFIGURED
    _CONFIGURED = True
    if config.option.basetemp is None:
        config.option.basetemp = str(ROOT / "pytest")


def pytest_sessionfinish(session):
    """A violation outside any test fails the run too."""
    _OUTSIDE_TESTS.extend(GUARD.take())
    if not _OUTSIDE_TESTS:
        return
    if session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    text = "hermetic tests: reached production outside any test\n" + production_guard.report(_OUTSIDE_TESTS)
    if reporter is not None:
        reporter.write_line(text, red=True)
    else:
        print(text)


def pytest_unconfigure(config):
    shutil.rmtree(ROOT)
    GUARD.active = False


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
def _no_live_obscura(monkeypatch):
    """obscura_available() asks this host's web stack on 127.0.0.1:8791. Every test gets False, which is what CI,
    coding jobs and the harness already get; a test's own stub applies on top.

    Workflow code that imports it lazily inside the sandbox gets a fresh module this stub does not reach; only a
    workflow runner that passes app.orchestrator.web_capability through reaches it, and the guard's socket rule
    (port 8791) refuses the call either way."""
    from app.orchestrator import web_capability

    monkeypatch.setattr(web_capability, "obscura_available", lambda: False)


@pytest.fixture(autouse=True)
def _no_host_secrets(monkeypatch):
    """The coding secret scan reads this host's env files, GitHub token and configs when it is not given known=.
    Tests get none, as coding units and CI do; a test of host-value detection passes known= itself."""
    from app.coding import workspace

    monkeypatch.setattr(workspace, "SECRET_FILES", [])
    monkeypatch.setattr(workspace, "SECRET_CONFIGS", [])


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
