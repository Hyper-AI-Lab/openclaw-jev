"""The production guard: its path core and every consumer's layout, its rules for paths, SQLite, processes, shell
strings, sockets and Temporal, and the hook the conftest installs."""
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.client import Client

from app import config
from tests.conftest import GUARD
from tests.production_guard import (
    DENIED_ROOTS, HOST_TOOLS, PATH_VARIABLES, TEMPORAL_CONNECT, Guard, ports, production_values, session_guard,
    suite_environment,
)

REPO = Path(__file__).resolve().parents[1]
DENIED_DIRS = [root for root in DENIED_ROOTS if root != "/run/rmp-code-reload.lock"]
INET, INET6, UNIX = (SimpleNamespace(family=family) for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX))
# server.py's POST /config restart of the gateway, run with shell=True.
CONFIG_RESTART = ("pkill -f 'openclaw-gateway' 2>/dev/null; sleep 2; "
                  "nohup openclaw gateway --force > /tmp/openclaw-gateway.log 2>&1 &")


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


def checking(tmp_path):
    """A guard over the real denied roots for pure checks: its repo is tmp_path/repo, and only tmp_path is allowed."""
    return Guard(DENIED_ROOTS, (tmp_path,), tmp_path / "repo")


def fakes(directory, names=HOST_TOOLS):
    """Harmless executables named like host tools; a check only looks them up."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        tool = directory / name
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return directory


def popen(argv, *, cwd=None, env=None):
    """The subprocess.Popen audit event of a call with this argv."""
    return "subprocess.Popen", (argv[0], argv, cwd, env)


@pytest.mark.parametrize("root", DENIED_ROOTS)
def test_check_refuses_a_read_and_a_write_under_each_denied_root(root, tmp_path):
    guard = checking(tmp_path)
    path = root if root == "/run/rmp-code-reload.lock" else f"{root}/hermetic-probe"
    assert guard.check("open", (path, "r", os.O_RDONLY)) == f"production path {path}"
    assert guard.check("open", (path, "w", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)) == f"production path {path}"
    assert guard.check("os.rename", (str(tmp_path / "new"), path, -1, -1)) == f"production path {path}"


def test_check_refuses_the_deploy_targets(tmp_path):
    guard = checking(tmp_path)
    assert guard.check("open", ("/etc/systemd/system/x.service", "w", os.O_WRONLY | os.O_CREAT))
    assert guard.check("os.listdir", ("/root/.cursor/rules",)) == "production path /root/.cursor/rules"
    assert guard.check("open", ("/run/rmp-code-reload.lock", "w", os.O_WRONLY | os.O_CREAT))


@pytest.mark.parametrize("allowed", [
    "/srv/aura-code/jobs/t", "/srv/aura-code/direct/t/s/repo", "/root/.openclaw/rmp",
])
def test_check_allows_an_allowed_repo_root_inside_a_denied_root_but_not_its_live_state(allowed):
    guard = Guard(DENIED_ROOTS, (allowed,), allowed)
    assert guard.check("open", (f"{allowed}/app/config.py", "r", os.O_RDONLY)) is None
    for name in ("data/last_health_canary.json", "settings.json"):
        assert guard.check("open", (f"{allowed}/{name}", "w", os.O_WRONLY)) == f"production path {allowed}/{name}"
    assert guard.check("open", (f"{os.path.dirname(allowed)}/other", "r", os.O_RDONLY))


@pytest.mark.parametrize("allowed", [
    "/srv/aura-code/cache/tmp", "/srv/aura-code/venvs/rmp", "/root/.openclaw/rmp/venv",
])
def test_check_allows_a_temp_dir_or_prefix_inside_a_denied_root(allowed, tmp_path):
    guard = Guard(DENIED_ROOTS, (allowed,), tmp_path / "repo")
    assert guard.check("open", (f"{allowed}/lib/x.py", "r", os.O_RDONLY)) is None
    assert guard.check("os.mkdir", (f"{allowed}/rmp-tests-x", 0o700, -1)) is None


def test_check_reads_the_database_of_a_sqlite_uri(tmp_path):
    guard = checking(tmp_path)
    live = "/root/.openclaw/state/openclaw.sqlite"
    assert guard.check("sqlite3.connect", (f"file:{live}?mode=ro",)) == f"production database {live}"
    assert guard.check("sqlite3.connect", (live,)) == f"production database {live}"
    assert guard.check("sqlite3.connect", (f"file:{tmp_path}/x.db?mode=rwc",)) is None
    assert guard.check("sqlite3.connect", (str(tmp_path / "x.db"),)) is None


def test_check_skips_fds_and_names_relative_to_a_directory_fd(tmp_path):
    guard = checking(tmp_path)
    assert guard.check("open", (3, "r", os.O_RDONLY)) is None
    assert guard.check("os.scandir", (3,)) is None
    assert guard.check("os.remove", ("data", 3)) is None
    # shutil.rmtree opens each subdirectory with os.open(name, dir_fd=...), whose event carries no dir_fd.
    assert guard.check("open", ("data", None, os.O_RDONLY)) is None
    assert guard.check("open", ("/etc/rmp/rmp.env", None, os.O_RDONLY)) == "production path /etc/rmp/rmp.env"


def test_check_resolves_a_relative_link_target_against_the_links_directory(tmp_path):
    guard = checking(tmp_path)
    link = str(tmp_path / "versions" / "link")
    assert guard.check("os.symlink", ("../versions/2.1.280", link, -1)) is None
    assert guard.check("os.symlink", ("/root/.openclaw/rmp", link, -1)) == "production path /root/.openclaw/rmp"


def test_check_reads_a_glob_pattern_and_an_archive_base_dir_relative_to_their_root_dir(tmp_path):
    guard = Guard(DENIED_ROOTS, (tmp_path,), tmp_path)
    assert guard.check("glob.glob/2", ("data/*", False, str(tmp_path), None)) == f"production path {tmp_path}/data/*"
    assert guard.check("glob.glob/2", ("app/*", False, str(tmp_path), None)) is None
    assert guard.check("shutil.make_archive", (str(tmp_path / "out"), "gztar", str(tmp_path), "data")) == (
        f"production path {tmp_path}/data")
    assert guard.check("shutil.make_archive", (str(tmp_path / "out"), "gztar", str(tmp_path), "app")) is None


def test_check_refuses_absolute_argv_paths_and_working_directories_but_not_paths_inside_options(tmp_path):
    guard = checking(tmp_path)
    assert guard.check(*popen(["true", "/etc/rmp"])) == "production path /etc/rmp"
    assert guard.check(*popen(["true"], cwd="/srv/aura-code/jobs")) == (
        "production working directory /srv/aura-code/jobs")
    assert guard.check(*popen(["/root/.openclaw/rmp/venv/bin/python", "-V"])) == (
        "production program /root/.openclaw/rmp/venv/bin/python")
    assert guard.check(*popen(["systemd-run-like", "-p", "InaccessiblePaths=/etc/rmp", "--x=/root/.openclaw"])) is None
    assert guard.check(*popen(["git", "-C", str(tmp_path / "repo"), "status"], cwd=str(tmp_path))) is None


@pytest.mark.parametrize("tool", sorted(HOST_TOOLS))
def test_check_refuses_a_host_tool_outside_the_repo_and_allows_a_fake_inside(tool, tmp_path):
    guard = checking(tmp_path)
    host, repo_fakes = fakes(tmp_path / "host", [tool]), fakes(tmp_path / "repo/tests/fakes", [tool])
    assert guard.check(*popen([tool, "x"], env={"PATH": str(host)})) == f"host tool {host / tool}"
    assert guard.check(*popen([str(host / tool), "x"])) == f"host tool {host / tool}"
    assert guard.check(*popen([tool, "x"], env={"PATH": f"{repo_fakes}{os.pathsep}{host}"})) is None
    assert guard.check(*popen([tool, "x"], env={b"PATH": os.fsencode(host)})) == f"host tool {host / tool}"
    # Not installed: there is nothing on this host for it to reach.
    assert guard.check(*popen([tool, "x"], env={"PATH": str(tmp_path / "nowhere")})) is None


def test_check_finds_the_program_behind_assignments_and_wrappers(tmp_path):
    guard = checking(tmp_path)
    host = fakes(tmp_path / "host")
    env = {"PATH": str(host)}
    assert guard.check(*popen(["nohup", "openclaw", "gateway"], env=env)) == f"host tool {host / 'openclaw'}"
    assert guard.check(*popen(["env", "A=1", "systemctl", "x"], env=env)) == f"host tool {host / 'systemctl'}"
    assert guard.check(*popen(["timeout", "-s", "KILL", "5", "nice", "-n", "10", "journalctl", "-n1"], env=env)) == (
        f"host tool {host / 'journalctl'}")
    assert guard.check(*popen(["sudo", "-u", "aura", "--", "docker", "ps"], env=env)) == f"host tool {host / 'docker'}"
    assert guard.check(*popen(["setsid", "-f", "/usr/bin/env", "-u", "X", "claude", "-p"], env=env)) == (
        f"host tool {host / 'claude'}")
    assert guard.check(*popen(["echo", "systemctl"], env=env)) is None
    assert guard.check(*popen(["timeout", "5", "true", "systemctl"], env=env)) is None


@pytest.mark.parametrize("script", [
    "systemctl --version", "true /etc/rmp", "pkill -f x", CONFIG_RESTART, "true && (journalctl -n1)",
    "echo $(systemctl x)", "echo `nft list ruleset`", "FOO=1 nohup openclaw gateway", "bash -c 'systemctl x'",
    "echo ok # a comment\nkillall x", "echo 'unclosed",
])
def test_a_shell_string_that_reaches_production_is_refused(script, tmp_path, monkeypatch):
    guard = checking(tmp_path)
    monkeypatch.setenv("PATH", f"{fakes(tmp_path / 'host')}{os.pathsep}{os.environ['PATH']}")
    assert guard.check("subprocess.Popen", ("/bin/sh", ["/bin/sh", "-c", script], None, None)) is not None
    assert guard.check("subprocess.Popen", ("bash", ["bash", "-ec", script], None, None)) is not None
    assert guard.check("os.system", (os.fsencode(script),)) is not None


@pytest.mark.parametrize("script", ["echo systemctl", "true; echo ok", "ls /tmp", "sh script.sh"])
def test_a_shell_string_that_stays_off_the_host_is_allowed(script, tmp_path, monkeypatch):
    guard = checking(tmp_path)
    monkeypatch.setenv("PATH", f"{fakes(tmp_path / 'host')}{os.pathsep}{os.environ['PATH']}")
    assert guard.check("subprocess.Popen", ("/bin/sh", ["/bin/sh", "-c", script], None, None)) is None
    assert guard.check("os.system", (os.fsencode(script),)) is None


def test_the_service_ports_are_the_ports_coding_units_may_not_reach():
    assert GUARD.service_ports == ports(config.DEFAULT_CODING["blocked_tcp_ports"])
    assert {5432, 6333, 6334, 7233, 7243, 8000, 8791, 18789, 18899} <= GUARD.service_ports
    assert ports([22, "6333-6334"]) == {22, 6333, 6334}


def test_check_refuses_this_hosts_services_on_loopback_only(tmp_path, monkeypatch):
    monkeypatch.delenv("RMP_QDRANT_IT", raising=False)
    guard = checking(tmp_path)
    for port in (5432, 6333, 6334, 7233, 7243, 8000, 8791, 18789, 18899):
        assert guard.check("socket.connect", (INET, ("127.0.0.1", port))) == f"this host's service on 127.0.0.1:{port}"
    for host in ("localhost", "127.0.0.53", "0.0.0.0"):
        assert guard.check("socket.connect", (INET, (host, 8791))) is not None
    for host in ("::1", "::ffff:127.0.0.1", "::"):
        assert guard.check("socket.connect", (INET6, (host, 7233, 0, 0))) is not None
    for port in (7232, 7244, 18788, 18900, 43123):
        assert guard.check("socket.connect", (INET, ("127.0.0.1", port))) is None
    for host in ("10.0.0.5", "93.184.216.34", "slack.com"):
        assert guard.check("socket.connect", (INET, (host, 8000))) is None
    assert guard.check("socket.connect", (UNIX, "/root/.openclaw/gateway.sock")) == (
        "production socket /root/.openclaw/gateway.sock")
    assert guard.check("socket.connect", (UNIX, str(tmp_path / "test.sock"))) is None
    assert guard.check(TEMPORAL_CONNECT, ("localhost:7233",)) == "this host's Temporal at localhost:7233"
    assert guard.check(TEMPORAL_CONNECT, ("http://[::1]:7233",)) is not None
    assert guard.check(TEMPORAL_CONNECT, ("127.0.0.1:43123",)) is None
    assert guard.check(TEMPORAL_CONNECT, ("temporal.example.com:7233",)) is None


def test_rmp_qdrant_it_lifts_the_qdrant_ports_only(tmp_path, monkeypatch):
    guard = checking(tmp_path)
    monkeypatch.setenv("RMP_QDRANT_IT", "1")
    assert guard.check("socket.connect", (INET, ("127.0.0.1", 6333))) is None
    assert guard.check("socket.connect", (INET, ("127.0.0.1", 6334))) is None
    assert guard.check("socket.connect", (INET, ("127.0.0.1", 8791))) is not None
    assert guard.check(TEMPORAL_CONNECT, ("localhost:7233",)) is not None


@pytest.mark.parametrize("root", DENIED_DIRS)
def test_the_installed_hook_refuses_and_records_reading_or_listing_a_denied_root(root):
    probe = f"{root}/hermetic-probe-{uuid.uuid4()}"
    with GUARD.expect_violations() as recorded:
        with pytest.raises(PermissionError, match="hermetic tests: production path"):
            open(probe)
        with pytest.raises(PermissionError, match="hermetic tests: production path"):
            os.listdir(root)
    assert [(v.event, v.target) for v in recorded] == [
        ("open", f"production path {probe}"), ("os.listdir", f"production path {root}")]
    assert recorded[0].stack[0].startswith("tests/test_production_guard.py:")


async def test_the_installed_hook_refuses_the_lock_sqlite_processes_shells_sockets_and_temporal(tmp_path,
                                                                                                 monkeypatch):
    # Stand-ins outside the repo: even a broken hook would run only these.
    host = fakes(tmp_path / "host", ["systemctl", "pkill"])
    monkeypatch.setenv("PATH", f"{host}{os.pathsep}{os.environ['PATH']}")
    probe = f"hermetic-probe-{uuid.uuid4()}"
    with GUARD.expect_violations() as recorded:
        with pytest.raises(PermissionError):
            open("/run/rmp-code-reload.lock")
        with pytest.raises(PermissionError):
            sqlite3.connect("file:/root/.openclaw/x?mode=ro", uri=True)
        with pytest.raises(PermissionError):
            subprocess.run(["true", "/etc/rmp"])
        with pytest.raises(PermissionError):
            subprocess.run("systemctl --version", shell=True)
        with pytest.raises(PermissionError):
            subprocess.run(["sh", "-c", "true /etc/rmp"])
        with pytest.raises(PermissionError):
            os.system(f"pkill -f {probe}")
        with socket.socket() as sock, pytest.raises(PermissionError):
            sock.connect(("127.0.0.1", 8791))
        with pytest.raises(RuntimeError, match="hermetic tests: this host's Temporal at localhost:7233"):
            await Client.connect("localhost:7233")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with socket.create_connection(listener.getsockname(), timeout=5):
                pass
    assert [(v.event, v.target) for v in recorded] == [
        ("open", "production path /run/rmp-code-reload.lock"),
        ("sqlite3.connect", "production database /root/.openclaw/x"),
        ("subprocess.Popen", "production path /etc/rmp"),
        ("subprocess.Popen", f"host tool {host / 'systemctl'}"),
        ("subprocess.Popen", "production path /etc/rmp"),
        ("os.system", f"host tool {host / 'pkill'}"),
        ("socket.connect", "this host's service on 127.0.0.1:8791"),
        (TEMPORAL_CONNECT, "this host's Temporal at localhost:7233"),
    ]


def test_a_refusal_the_code_swallowed_fails_its_test_and_one_outside_any_test_fails_the_run(tmp_path):
    # The child's tests are inside its temp dir: in a coding job tmp_path is under /srv/aura-code, where the child's
    # guard allows only its own temp dir.
    run = tmp_path / "run"
    run.mkdir()
    (run / "test_swallowed.py").write_text(
        "def test_swallows_the_refusal():\n"
        "    try:\n"
        "        open('/etc/rmp/hermetic-probe')\n"
        "    except OSError:\n"
        "        pass\n")
    (run / "test_collected.py").write_text(
        "import os\n"
        "try:\n"
        "    os.listdir('/root/.openclaw')\n"
        "except OSError:\n"
        "    pass\n\n\n"
        "def test_nothing():\n"
        "    pass\n")
    env = {name: value for name, value in os.environ.items() if name not in PATH_VARIABLES}
    env["TMPDIR"] = str(tmp_path)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.conftest",
                           "--rootdir", str(run), str(run)], cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=120)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, output
    assert "1 error" in output and "ERROR at teardown of test_swallows_the_refusal" in output
    assert "open: production path /etc/rmp/hermetic-probe" in output
    assert "reached production outside any test" in output and "os.listdir: production path /root/.openclaw" in output
    assert list(tmp_path.iterdir()) == [run]
