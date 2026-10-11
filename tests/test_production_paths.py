"""Host paths: production keeps its values, and under the test environment none of them points at production."""
import dataclasses
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import config
from app.coding import deploy, direct
from app.llm import quota_broker
from app.production import canary_sentinel
from tests import production_guard
from tests.production_guard import PATH_VARIABLES, suite_environment

REPO = Path(__file__).resolve().parents[1]
# Every host path a module reads from config, with its value in production (main 5424c02).
PRODUCTION = {
    "app.config.OPENCLAW_ENV_PATH": "/etc/openclaw/openclaw.env",
    "app.llm.quota_broker.OPENCLAW_ENV_PATH": "/etc/openclaw/openclaw.env",
    "app.memory.vector.OPENCLAW_ENV_PATH": "/etc/openclaw/openclaw.env",
    "app.llm.quota_broker.OPENCLAW_STATE_DB": "/root/.openclaw/state/openclaw.sqlite",
    "app.production.canary_sentinel.HEALTH_CANARY_PATH": "/root/.openclaw/rmp/data/last_health_canary.json",
    "app.production.canary_sentinel.MEMORY_CANARY_PATH": "/root/.openclaw/rmp/data/last_memory_canary.json",
    "app.production.canary_sentinel.ALERT_STATE_PATH": "/root/.openclaw/rmp/data/last_canary_alert.json",
    "app.production.canary_sentinel.REMEDIATION_STATE_PATH": "/root/.openclaw/rmp/data/last_canary_remediation.json",
    "app.production.canary_sentinel.CANARY_SCRIPT": "/root/.openclaw/rmp/ops/canary.sh",
    "app.config.AURA_CODE_ROOT": "/srv/aura-code",
    "app.coding.units.CODE_ROOT": "/srv/aura-code",
    "app.coding.units.JOBS_DIR": "/srv/aura-code/jobs",
    "app.coding.units.RUNS_DIR": "/srv/aura-code/runs",
    "app.coding.units.CACHE_DIR": "/srv/aura-code/cache",
    "app.coding.units.VENVS_DIR": "/srv/aura-code/venvs",
    "app.coding.units.CODING_POLICY_DIR": "/srv/aura-code/policy",
    "app.coding.units.MANAGED_SETTINGS": "/srv/aura-code/policy/managed-settings.json",
    "app.coding.deploy.BUNDLES_DIR": "/srv/aura-code/bundles",
    "app.coding.verify.VENVS_DIR": "/srv/aura-code/venvs",
    "app.coding.verify.CACHE_DIR": "/srv/aura-code/cache",
    "app.coding.verify.TEST_TMP": "/srv/aura-code/cache/tmp",
    "app.coding.workspace.REPOS_DIR": "/srv/aura-code/repos",
    "app.coding.workspace.REVIEW_DIR": "/srv/aura-code/review",
    "app.coding.direct.DIRECT_DIR": "/srv/aura-code/direct",
    "app.coding.deploy.LIVE_REPO": "/root/.openclaw/rmp",
}
# Run with every production default in effect, so the guard's audit hook refuses and records any touch of a production
# path, database, process or service while the modules import: a later import-time settings read would otherwise read,
# and could write, the live ones.
CHILD = r"""
import importlib, json, sys
from tests.production_guard import DENIED_ROOTS, Guard

repo, tmp, names = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
guard = Guard(DENIED_ROOTS, (repo, sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix, tmp), repo)
# Not install(), which imports app.config before the hook is in place; a socket event loads the ports on first use.
sys.addaudithook(guard.hook)
values = {}
try:
    for name in names:
        module, _, attr = name.rpartition(".")
        values[name] = str(getattr(importlib.import_module(module), attr))
finally:
    print(json.dumps({"values": values, "violations": [f"{v.event} {v.target}" for v in guard.violations]}))
sys.exit(1 if guard.violations else 0)
"""


def constant(name):
    module, _, attr = name.rpartition(".")
    return getattr(importlib.import_module(module), attr)


def test_the_production_defaults_are_unchanged(tmp_path):
    # DATABASE_URL stays: without it app.db.database falls back to /etc/rmp/rmp.env.
    env = {name: value for name, value in os.environ.items() if name not in PATH_VARIABLES}
    env["TMPDIR"] = str(tmp_path)
    proc = subprocess.run([sys.executable, "-c", CHILD, str(REPO), str(tmp_path), json.dumps(list(PRODUCTION))],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert proc.stdout, proc.stderr
    result = json.loads(proc.stdout.splitlines()[-1])
    assert result["violations"] == []
    assert proc.returncode == 0, proc.stderr
    assert result["values"] == PRODUCTION


def test_under_the_test_environment_nothing_resolves_to_production(monkeypatch):
    guard = production_guard.session_guard(REPO)
    values = {name: constant(name) for name in PRODUCTION}
    # This test's DIRECT_DIR is the conftest's per-test directory; Turn's default kept the value import resolved.
    values["app.coding.direct.Turn.root"] = {f.name: f.default for f in dataclasses.fields(direct.Turn)}["root"]
    assert {name: value for name, value in values.items() if guard.denies(value)} == {}

    monkeypatch.setattr(quota_broker, "USE_SQLITE_AUTH", None)
    assert quota_broker.OPENCLAW_STATE_DB == Path(config.OPENCLAW_HOME) / "state" / "openclaw.sqlite"
    assert quota_broker._use_sqlite_auth() is False
    for path in (canary_sentinel.HEALTH_CANARY_PATH, canary_sentinel.MEMORY_CANARY_PATH,
                 canary_sentinel.ALERT_STATE_PATH, canary_sentinel.REMEDIATION_STATE_PATH):
        assert path.parent == Path(config.RMP_DATA_DIR)
        assert not path.is_relative_to(REPO)
    assert deploy.LIVE_REPO == Path(config.OPENCLAW_HOME) / "rmp"


@pytest.mark.parametrize("inherited", ["OPENCLAW_HOME", "RMP_ROOT", "RMP_DATA_DIR", "RMP_SETTINGS_PATH",
                                       "OPENCLAW_ENV_PATH", "AURA_CODE_ROOT"])
def test_each_variable_defaults_on_its_own(inherited, tmp_path):
    root, repo = tmp_path / "root", tmp_path / "repo"
    defaults = {
        "OPENCLAW_HOME": str(root / "openclaw"),
        "RMP_ROOT": str(repo),
        "RMP_DATA_DIR": str(root / "data"),
        "RMP_SETTINGS_PATH": str(root / "settings.json"),
        "OPENCLAW_ENV_PATH": str(root / "openclaw.env"),
        "AURA_CODE_ROOT": str(root / "aura-code"),
    }
    expected = {name: value for name, value in defaults.items() if name != inherited}
    expected["DATABASE_URL"] = f"sqlite+aiosqlite:///{root / 'unmocked.db'}"
    assert suite_environment({inherited: str(tmp_path / "inherited")}, root, repo) == expected


def test_a_production_value_is_refused_before_anything_is_created(tmp_path):
    env = {name: value for name, value in os.environ.items() if name not in PATH_VARIABLES}
    env.update(OPENCLAW_HOME="/root/.openclaw", TMPDIR=str(tmp_path))
    proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                           "tests/test_production_paths.py"], cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=120)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 4, output
    assert "OPENCLAW_HOME=/root/.openclaw points at production" in output
    assert list(tmp_path.iterdir()) == []


# pytest in a child that names the root its conftest made, then exits with pytest's code.
STOPPED_EARLY = "import sys, pytest; code = pytest.main(sys.argv[1:]); print(sys.modules['tests.conftest'].ROOT); " \
                "sys.exit(code)"
# A test that leaves a thread running, which writes into the root once pytest has removed it.
WRITER = '''
import threading
import time

from tests.conftest import ROOT


def test_leaves_a_writer_running():
    def write_once_the_root_is_gone():
        deadline = time.monotonic() + 30
        while ROOT.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        (ROOT / "late").mkdir(parents=True)

    threading.Thread(target=write_once_the_root_is_gone).start()
'''


# pytest 9 answers a single --version before it imports a conftest. Given twice, it imports them and stops before
# configuring, as a single --version does on pytest 8.
@pytest.mark.parametrize("args, code", [(["--version", "--version"], 0), (["--no-such-option"], 4)],
                         ids=["version", "usage-error"])
def test_the_root_is_removed_when_pytest_stops_before_configuring(args, code, tmp_path):
    env = {name: value for name, value in os.environ.items() if name not in PATH_VARIABLES}
    env["TMPDIR"] = str(tmp_path)
    proc = subprocess.run([sys.executable, "-c", STOPPED_EARLY, "-p", "no:cacheprovider", *args], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == code, proc.stdout + proc.stderr
    # The conftest made its root in the child's temp dir, and nothing is left there.
    assert Path(proc.stdout.splitlines()[-1]).parent == tmp_path
    assert list(tmp_path.iterdir()) == []


def test_a_writer_left_running_by_a_test_still_leaves_the_root_behind(tmp_path):
    # The child's test is inside its temp dir: in a coding job tmp_path is under /srv/aura-code, where the child's
    # guard allows only its own temp dir.
    run = tmp_path / "run"
    run.mkdir()
    (run / "test_writer.py").write_text(WRITER)
    env = {name: value for name, value in os.environ.items() if name not in PATH_VARIABLES}
    env["TMPDIR"] = str(tmp_path)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.conftest",
                           "--rootdir", str(run), str(run)], cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The removal at exit is only for a run that never configured: this leak stays visible.
    [root] = [path for path in tmp_path.iterdir() if path != run]
    assert root.name.startswith("rmp-tests-") and [p.name for p in root.iterdir()] == ["late"]
