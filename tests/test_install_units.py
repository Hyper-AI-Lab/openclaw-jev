"""ops/install_units.py, the operator's install of the unit files tracked in ops/systemd/.

git runs for real on a repo in tmp_path; the unit target, the backup root and the code-reload lock are in tmp_path too,
and systemctl is a recording fake.
"""
import fcntl
import importlib.util
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.coding import deploy
from tests.conftest import GUARD

ROOT = Path(__file__).resolve().parents[1]
ENABLED = "# Units that must be enabled on this host.\nrmp-a.service\nrmp-b.timer\nrmp-c.service\nrmp-d.service\n"
TIMER = "[Unit]\nDescription=b\n\n[Timer]\nOnCalendar=daily\n\n[Install]\nWantedBy=timers.target\n"
# What systemctl is-enabled answers before the install; rmp-b.timer has no file yet, so it is not-found.
STATES = {"rmp-a.service": "enabled", "rmp-c.service": "enabled", "rmp-d.service": "disabled"}


def load():
    module_spec = importlib.util.spec_from_file_location("install_units", ROOT / "ops" / "install_units.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def unit(description: str) -> str:
    return (f"[Unit]\nDescription={description}\n\n[Service]\nExecStart=/bin/true\n\n"
            "[Install]\nWantedBy=multi-user.target\n")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict, message: str) -> str:
    """Commit ``files`` under ops/systemd/; the commit as git log's %h and %s print it."""
    for name, text in files.items():
        (repo / "ops" / "systemd" / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / "ops" / "systemd" / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return f"{git(repo, 'rev-parse', '--short', 'HEAD')} {message}"


def install(target: Path, name: str, text: str) -> None:
    """A unit file as systemd's directory holds it, whatever the umask: RMP's own test runs use 0077."""
    if not (target / name).parent.is_dir():
        (target / name).parent.mkdir()
        (target / name).parent.chmod(0o755)
    (target / name).write_text(text)
    (target / name).chmod(0o644)


class FakeSystemctl:
    """is-enabled answers from a table, not-found for a unit it lacks; daemon-reload applies the states the reload
    shows, and enable marks its unit enabled. Every call is recorded; a call in ``failing`` exits 1."""

    def __init__(self, states=STATES, *, reloaded=None, failing=()):
        self.states, self.reloaded, self.failing, self.calls = dict(states), dict(reloaded or {}), set(failing), []

    def __call__(self, *args):
        self.calls.append(args)
        if args in self.failing:
            return 1, ""
        if args[0] == "is-enabled":
            state = self.states.get(args[1], "not-found")
            return (0 if state == "enabled" else 1), f"{state}\n"
        if args[0] == "daemon-reload":
            self.states.update(self.reloaded)
        elif args[0] == "enable":
            self.states[args[1]] = "enabled"
        return 0, ""


@pytest.fixture
def host(tmp_path):
    """HEAD changes rmp-a.service and rmp-d.service's drop-in, adds the b timer and its service, and adds a drop-in for
    rmp-c.service, none of them installed yet. rmp-c.service and rmp-d.service are installed as committed;
    rmp-old.service is installed with no tracked file. Beside them: another unit and a .wants/ link."""
    repo, target = tmp_path / "repo", tmp_path / "etc-systemd-system"
    git(tmp_path, "init", "-q", str(repo))
    first = {"rmp-a.service": unit("a"), "rmp-c.service": unit("c"), "rmp-d.service": unit("d"),
             "rmp-d.service.d/05-nice.conf": "[Service]\nNice=5\n"}
    commit(repo, {**first, "enabled-units.txt": ENABLED}, "Track the units")
    target.mkdir()
    target.chmod(0o755)
    for name, text in {**first, "rmp-old.service": unit("old"), "ssh.service": unit("ssh")}.items():
        install(target, name, text)
    (target / "multi-user.target.wants").mkdir()
    (target / "multi-user.target.wants" / "rmp-a.service").symlink_to(target / "rmp-a.service")
    commits = {
        "a": commit(repo, {"rmp-a.service": unit("a, bounded")}, "Bound the a service"),
        "b": commit(repo, {"rmp-b.service": unit("b"), "rmp-b.timer": TIMER}, "Add the b timer"),
        "c": commit(repo, {"rmp-c.service.d/10-limits.conf": "[Service]\nMemoryMax=1G\n"}, "Limit the c service"),
        "d": commit(repo, {"rmp-d.service.d/05-nice.conf": "[Service]\nNice=10\n"}, "Renice the d service"),
    }
    return SimpleNamespace(repo=repo, target=target, backups=tmp_path / "backups", lock=tmp_path / "code-reload.lock",
                           commits=commits)


def run(host, *argv, systemctl, active_user_tasks=lambda: 0):
    return load().main(list(argv), repo=host.repo, target=host.target, backup_root=host.backups, lock=host.lock,
                       systemctl=systemctl, active_user_tasks=active_user_tasks)


def snapshot(root: Path) -> dict:
    """Everything under ``root``: a file's bytes and mode, a directory's mode, a symlink's target."""
    found = {".": ("dir", root.stat().st_mode & 0o777)}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[name] = ("link", os.readlink(path))
        elif path.is_dir():
            found[name] = ("dir", path.stat().st_mode & 0o777)
        else:
            found[name] = (path.read_bytes(), path.stat().st_mode & 0o777)
    return found


def committed(host, name: str) -> bytes:
    return subprocess.run(["git", "show", f"HEAD:ops/systemd/{name}"], cwd=host.repo, check=True,
                          capture_output=True).stdout


def test_a_dry_run_names_each_file_with_its_commit_and_each_unit_to_enable_and_writes_nothing(host, capsys):
    before = snapshot(host.target)
    systemctl = FakeSystemctl()
    assert run(host, systemctl=systemctl) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"changed: rmp-a.service ({host.commits['a']})",
        f"new: rmp-b.service ({host.commits['b']})",
        f"new: rmp-b.timer ({host.commits['b']})",
        f"new: rmp-c.service.d/10-limits.conf ({host.commits['c']})",
        f"changed: rmp-d.service.d/05-nice.conf ({host.commits['d']})",
        "host-only: rmp-old.service",
        "not enabled: rmp-b.timer",
        "not enabled: rmp-d.service",
    ]
    assert snapshot(host.target) == before and not host.backups.exists() and not host.lock.exists()
    assert systemctl.calls == [("is-enabled", u) for u in ("rmp-a.service", "rmp-b.timer", "rmp-c.service",
                                                           "rmp-d.service")]


def test_apply_installs_heads_bytes_backs_up_reloads_once_and_enables_without_starting(host, capsys):
    old = {name: (host.target / name).read_bytes() for name in ("rmp-a.service", "rmp-d.service.d/05-nice.conf")}
    # Neither an uncommitted edit nor an untracked file reaches the host.
    (host.repo / "ops" / "systemd" / "rmp-a.service").write_text(unit("a, edited and not committed"))
    (host.repo / "ops" / "systemd" / "rmp-e.service").write_text(unit("e, never committed"))
    systemctl = FakeSystemctl(reloaded={"rmp-b.timer": "disabled"})
    assert run(host, "--apply", systemctl=systemctl) == 0

    installed = ["rmp-a.service", "rmp-b.service", "rmp-b.timer", "rmp-c.service.d/10-limits.conf",
                 "rmp-d.service.d/05-nice.conf"]
    for name in installed:
        assert (host.target / name).read_bytes() == committed(host, name), name
        assert (host.target / name).stat().st_mode & 0o777 == 0o644, name
    assert (host.target / "rmp-c.service.d").stat().st_mode & 0o777 == 0o755
    assert not (host.target / "rmp-e.service").exists()
    [backup] = host.backups.iterdir()
    backed_up = {path.relative_to(backup).as_posix(): path.read_bytes() for path in backup.rglob("*") if path.is_file()}
    assert backed_up == old
    assert host.backups.stat().st_mode & 0o777 == 0o700
    listed = [("is-enabled", u) for u in ("rmp-a.service", "rmp-b.timer", "rmp-c.service", "rmp-d.service")]
    assert systemctl.calls == [*listed, ("daemon-reload",), *listed, ("enable", "rmp-b.timer"),
                               ("enable", "rmp-d.service")]
    target = host.target
    assert capsys.readouterr().out.splitlines() == [
        f"installed: {', '.join(installed)}",
        "enabled: rmp-b.timer, rmp-d.service",
        "start: rmp-b.timer",
        "start: rmp-d.service",
        f"backup: {backup}",
        f"rollback: systemctl disable rmp-b.timer rmp-d.service && cp -a {backup}/. {target}/ && rm -f "
        f"{target}/rmp-b.service {target}/rmp-b.timer {target}/rmp-c.service.d/10-limits.conf "
        "&& systemctl daemon-reload",
        "Each changed unit takes effect at its next start; restart one now only in an idle window. Start each unit on "
        "a start: line in an idle window.",
    ]


def test_the_rollback_line_puts_back_every_file_and_mode_it_replaced(host, capsys):
    before = snapshot(host.target)
    assert run(host, "--apply", systemctl=FakeSystemctl()) == 0
    rollback = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("rollback: "))
    for step in rollback.removeprefix("rollback: ").split(" && "):
        if not step.startswith("systemctl "):
            subprocess.run(shlex.split(step), check=True)
    # The drop-in directory the install created stays, empty.
    assert snapshot(host.target) == {**before, "rmp-c.service.d": ("dir", 0o755)}


def unknown():
    raise RuntimeError("the database is down")


@pytest.mark.parametrize("active, refusal", [
    (lambda: 1, "refused: 1 user task(s) active; run again when Aura is idle"),
    (unknown, "refused: could not count active user tasks (the database is down)"),
], ids=["busy", "unknown"])
def test_apply_refuses_unless_aura_is_known_to_be_idle(host, capsys, active, refusal):
    before = snapshot(host.target)
    systemctl = FakeSystemctl()
    assert run(host, "--apply", systemctl=systemctl, active_user_tasks=active) == 2
    assert capsys.readouterr().out.splitlines() == [refusal]
    assert snapshot(host.target) == before and not host.backups.exists() and systemctl.calls == []


def test_apply_refuses_while_a_deploy_or_code_reload_holds_the_lock(host, capsys):
    before = snapshot(host.target)
    systemctl, counted = FakeSystemctl(), []
    with open(host.lock, "w") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        assert run(host, "--apply", systemctl=systemctl, active_user_tasks=lambda: counted.append(1) or 0) == 2
    assert capsys.readouterr().out.splitlines() == [f"refused: a deploy or code reload holds {host.lock}"]
    assert snapshot(host.target) == before and not host.backups.exists() and systemctl.calls == [] and counted == []


def test_nothing_to_install_reloads_nothing(host, capsys):
    for name in ("rmp-a.service", "rmp-b.service", "rmp-b.timer", "rmp-c.service.d/10-limits.conf",
                 "rmp-d.service.d/05-nice.conf"):
        install(host.target, name, committed(host, name).decode())
    (host.target / "rmp-old.service").unlink()
    states = {**STATES, "rmp-b.timer": "enabled", "rmp-d.service": "enabled"}
    assert run(host, systemctl=FakeSystemctl(states)) == 0
    assert capsys.readouterr().out == "nothing to install\n"

    systemctl = FakeSystemctl(states)
    assert run(host, "--apply", systemctl=systemctl) == 0
    assert [call for call in systemctl.calls if call[0] != "is-enabled"] == []
    [backup] = host.backups.iterdir()
    assert list(backup.iterdir()) == []
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "nothing to install" and out[1] == f"backup: {backup}"
    assert out[2] == f"rollback: cp -a {backup}/. {host.target}/ && systemctl daemon-reload"


def test_a_masked_or_static_listed_unit_is_left_as_it_is(host, capsys):
    states = {**STATES, "rmp-c.service": "masked", "rmp-d.service": "static"}
    left = ["left as is: rmp-c.service (masked)", "left as is: rmp-d.service (static)"]
    assert run(host, systemctl=FakeSystemctl(states)) == 0
    assert [line for line in capsys.readouterr().out.splitlines() if line.startswith("left as is: ")] == left

    systemctl = FakeSystemctl(states, reloaded={"rmp-b.timer": "disabled"})
    assert run(host, "--apply", systemctl=systemctl) == 0
    assert [call for call in systemctl.calls if call[0] == "enable"] == [("enable", "rmp-b.timer")]
    assert [line for line in capsys.readouterr().out.splitlines() if line.startswith("left as is: ")] == left


def test_a_failing_daemon_reload_stops_before_any_enable(host, capsys):
    systemctl = FakeSystemctl(failing=[("daemon-reload",)])
    assert run(host, "--apply", systemctl=systemctl) == 1
    assert systemctl.calls[-1] == ("daemon-reload",) and not [call for call in systemctl.calls if call[0] == "enable"]
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["failed: systemctl daemon-reload (exit 1)", "installed: rmp-a.service, rmp-b.service, "
                       "rmp-b.timer, rmp-c.service.d/10-limits.conf, rmp-d.service.d/05-nice.conf"]
    [backup] = host.backups.iterdir()
    assert f"rollback: cp -a {backup}/. {host.target}/ && rm -f {host.target}/rmp-b.service" in "\n".join(out)
    assert (host.target / "rmp-b.timer").read_bytes() == committed(host, "rmp-b.timer")


def test_a_failing_enable_stops_there_and_its_rollback_disables_what_was_enabled(host, capsys):
    systemctl = FakeSystemctl(reloaded={"rmp-b.timer": "disabled"}, failing=[("enable", "rmp-d.service")])
    assert run(host, "--apply", systemctl=systemctl) == 1
    assert systemctl.calls[-2:] == [("enable", "rmp-b.timer"), ("enable", "rmp-d.service")]
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "failed: systemctl enable rmp-d.service (exit 1)"
    assert "enabled: rmp-b.timer" in out and "start: rmp-b.timer" in out and "start: rmp-d.service" not in out
    assert next(line for line in out if line.startswith("rollback: ")).startswith(
        "rollback: systemctl disable rmp-b.timer && cp -a ")


def test_the_defaults_are_the_hosts_and_the_guard_denies_each():
    install_units = load()
    assert install_units.BACKUP_ROOT == Path("/var/backups/rmp-units")
    for path in (install_units.BACKUP_ROOT, deploy.UNIT_TARGET, deploy.CODE_RELOAD_LOCK):
        assert GUARD.denies(path), path
