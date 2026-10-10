"""The production guard's path core: denied and allowed roots, the conftest's refusal, every consumer's layout."""
import os
import re
from pathlib import Path

import pytest

from app import config
from tests.production_guard import (
    DENIED_ROOTS, PATH_VARIABLES, Guard, production_values, session_guard, suite_environment,
)


def relocated(x):
    """The denied roots moved under ``x``, so a test can create paths in them without touching production."""
    return tuple(f"{x}{root}" for root in DENIED_ROOTS)


@pytest.mark.parametrize("root", DENIED_ROOTS)
def test_a_path_under_each_denied_root_is_denied(root, tmp_path):
    guard = Guard(DENIED_ROOTS, (tmp_path,), tmp_path / "repo")
    probe = f"{root}/hermetic-probe"
    assert guard.denies(root)
    assert guard.denies(probe) and guard.denies(os.fsencode(probe)) and guard.denies(Path(probe))
    assert not guard.denies(f"{root}-sibling/hermetic-probe")


@pytest.mark.parametrize("allowed", [
    "/srv/aura-code/jobs/t", "/srv/aura-code/cache/tmp", "/srv/aura-code/venvs/rmp",
    "/srv/aura-code/direct/t/s/repo", "/root/.openclaw/rmp", "/root/.openclaw/rmp/venv",
])
def test_an_allowed_root_inside_a_denied_root_is_allowed_and_nothing_beside_it(allowed):
    guard = Guard(DENIED_ROOTS, (allowed,), "/nonexistent/repo")
    assert not guard.denies(allowed)
    assert not guard.denies(f"{allowed}/lib/module.py")
    assert guard.denies(f"{allowed}-sibling")
    assert guard.denies(os.path.dirname(allowed))


def test_the_repo_roots_live_state_is_denied_inside_the_allowed_repo_root(tmp_path):
    repo = tmp_path / "repo"
    guard = Guard(DENIED_ROOTS, (repo,), repo)
    for path in ("data", "data/last_health_canary.json", "settings.json", "settings.json.lock"):
        assert guard.denies(repo / path), path
    for path in ("app/config.py", "settings.example.json", "database.md", "tests/fixtures/data"):
        assert not guard.denies(repo / path), path


def test_the_code_reload_lock_is_denied_and_the_rest_of_run_is_not(tmp_path):
    guard = Guard(DENIED_ROOTS, (tmp_path,), tmp_path / "repo")
    assert guard.denies("/run/rmp-code-reload.lock")
    assert not guard.denies("/run/other")
    assert not guard.denies("/run/rmp-code-reload.lock.d")


def test_a_symlink_from_an_allowed_root_into_a_denied_root_is_denied(tmp_path):
    repo, live = tmp_path / "repo", tmp_path / "root/.openclaw/rmp"
    repo.mkdir()
    live.mkdir(parents=True)
    (repo / "live").symlink_to(live)
    guard = Guard(relocated(tmp_path), (repo,), repo)
    assert guard.denies(repo / "live")
    assert guard.denies(repo / "live" / "app" / "config.py")
    assert not guard.denies(repo / "app" / "config.py")


@pytest.mark.parametrize("which", ["repo root", "prefix", "temp dir"])
def test_an_allowed_root_that_contains_a_denied_root_is_refused(which, tmp_path):
    roots = {"repo root": tmp_path / "repo", "prefix": tmp_path / "venv", "temp dir": tmp_path / "tmp"}
    roots[which] = tmp_path / "root"
    refusal = f"{tmp_path / 'root'} contains {tmp_path / 'root/.openclaw'}"
    with pytest.raises(pytest.UsageError, match=re.escape(refusal)):
        session_guard(roots["repo root"], prefixes=(roots["prefix"],), tempdir=roots["temp dir"],
                      denied_roots=relocated(tmp_path))


def test_a_temp_dir_that_is_a_denied_root_is_refused(tmp_path):
    with pytest.raises(pytest.UsageError, match="srv/aura-code contains"):
        session_guard(tmp_path / "repo", prefixes=(), tempdir=tmp_path / "srv/aura-code",
                      denied_roots=relocated(tmp_path))


def test_path_variables_names_every_variable_config_reads_a_path_from(monkeypatch, tmp_path):
    for name in PATH_VARIABLES:
        monkeypatch.setenv(name, str(tmp_path / name))
    for attr, resolver in config._PATH_ATTR_RESOLVERS.items():
        assert Path(resolver()).is_relative_to(tmp_path), f"{attr} reads a variable PATH_VARIABLES lacks"


LAYOUTS = {
    "coding job": ("srv/aura-code/jobs/t", "srv/aura-code/cache/tmp", "srv/aura-code/venvs/rmp"),
    "direct session": ("srv/aura-code/direct/t/s/repo", "tmp", "root/.openclaw/rmp/venv"),
    "go-live": ("root/.openclaw/rmp", "tmp", "root/.openclaw/rmp/venv"),
}


@pytest.mark.parametrize("layout", LAYOUTS)
def test_each_consumer_accepts_the_suite_defaults_and_refuses_an_inherited_production_value(layout, tmp_path):
    repo, tempdir, prefix = (tmp_path / part for part in LAYOUTS[layout])
    guard = session_guard(repo, prefixes=(prefix,), tempdir=tempdir, denied_roots=relocated(tmp_path))
    environment = suite_environment({}, tempdir / "rmp-tests-layout", repo)
    assert production_values(environment, guard) == []
    assert not guard.denies(environment["DATABASE_URL"].removeprefix("sqlite+aiosqlite:///"))
    inherited = {"OPENCLAW_HOME": str(tmp_path / "root/.openclaw")}
    assert [name for name, _ in production_values(inherited, guard)] == ["OPENCLAW_HOME"]
    if layout == "go-live":
        assert guard.denies(repo / "data") and guard.denies(repo / "settings.json")
