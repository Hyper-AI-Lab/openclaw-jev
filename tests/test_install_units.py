"""ops/install_units.py, the operator's install of the unit files tracked in ops/systemd/.

git runs for real on a repo in tmp_path; the unit target, the backup root and the code-reload lock are in tmp_path too,
and systemctl is a recording fake.
"""
import errno
import fcntl
import importlib.util
import os
import shlex
import shutil
import subprocess
from datetime import datetime
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
    shows, and enable marks its unit enabled. Every call is recorded; a call in ``failing`` exits 1, and one in
    ``timing_out`` raises what the real wrapper's subprocess.run raises after its 300 s."""

    def __init__(self, states=STATES, *, reloaded=None, failing=(), timing_out=()):
        self.states, self.reloaded, self.failing, self.calls = dict(states), dict(reloaded or {}), set(failing), []
        self.timing_out = set(timing_out)

    def __call__(self, *args):
        self.calls.append(args)
        if args in self.failing:
            return 1, ""
        if args in self.timing_out:
            raise subprocess.TimeoutExpired(["systemctl", *args], 300)
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
                           commits=commits, head=git(repo, "rev-parse", "HEAD"))


def run(host, *argv, systemctl, active_user_tasks=lambda: 0, module=None):
    return (module or load()).main(list(argv), repo=host.repo, target=host.target, backup_root=host.backups,
                                   lock=host.lock, systemctl=systemctl, active_user_tasks=active_user_tasks)


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


def roll_back(out: list) -> None:
    """Run the file steps of the printed rollback: line; the systemctl steps are the operator's."""
    rollback = next(line for line in out if line.startswith("rollback: "))
    for step in rollback.removeprefix("rollback: ").split(" && "):
        if not step.startswith("systemctl "):
            subprocess.run(shlex.split(step), check=True)


def test_a_dry_run_names_each_file_with_its_commit_and_each_unit_to_enable_and_writes_nothing(host, capsys):
    before = snapshot(host.target)
    systemctl = FakeSystemctl()
    assert run(host, systemctl=systemctl) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"head: {host.head}",
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
    assert run(host, "--apply", "--head", host.head, systemctl=systemctl) == 0

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
        f"head: {host.head}",
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
    assert run(host, "--apply", "--head", host.head, systemctl=FakeSystemctl()) == 0
    roll_back(capsys.readouterr().out.splitlines())
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
    assert run(host, "--apply", "--head", host.head, systemctl=systemctl, active_user_tasks=active) == 2
    assert capsys.readouterr().out.splitlines() == [refusal]
    assert snapshot(host.target) == before and not host.backups.exists() and systemctl.calls == []


def test_apply_refuses_while_a_deploy_or_code_reload_holds_the_lock(host, capsys):
    before = snapshot(host.target)
    systemctl, counted = FakeSystemctl(), []
    with open(host.lock, "w") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        assert run(host, "--apply", "--head", host.head, systemctl=systemctl,
                   active_user_tasks=lambda: counted.append(1) or 0) == 2
    assert capsys.readouterr().out.splitlines() == [f"refused: a deploy or code reload holds {host.lock}"]
    assert snapshot(host.target) == before and not host.backups.exists() and systemctl.calls == [] and counted == []


USAGE = "usage: install_units.py [--apply --head <sha>], <sha> being the commit on the reviewed dry run's head: line"


@pytest.mark.parametrize("given", ["missing", "a name", "stale"])
def test_apply_installs_only_the_reviewed_commit(host, capsys, given):
    """A deploy lands between the reviewed dry run and --apply: --apply refuses without the dry run's head, with a
    name that follows HEAD, and with the reviewed head once HEAD has moved on."""
    reviewed = host.head
    commit(host.repo, {"rmp-a.service": unit("a, changed after the review")}, "Change a after the review")
    head = git(host.repo, "rev-parse", "HEAD")
    argv, refusal = {
        "missing": (["--apply"], USAGE),
        "a name": (["--apply", "--head", "HEAD"], USAGE),
        "stale": (["--apply", "--head", reviewed], f"refused: HEAD is {head}, the reviewed dry run was {reviewed}"),
    }[given]
    before = snapshot(host.target)
    systemctl, counted = FakeSystemctl(), []
    assert run(host, *argv, systemctl=systemctl, active_user_tasks=lambda: counted.append(1) or 0) == 2
    assert capsys.readouterr().out.splitlines() == [refusal]
    assert snapshot(host.target) == before and not host.backups.exists() and systemctl.calls == [] and counted == []
    # The arguments are checked before the lock is taken.
    assert host.lock.exists() == (given == "stale")


def test_nothing_to_install_reloads_nothing(host, capsys):
    for name in ("rmp-a.service", "rmp-b.service", "rmp-b.timer", "rmp-c.service.d/10-limits.conf",
                 "rmp-d.service.d/05-nice.conf"):
        install(host.target, name, committed(host, name).decode())
    (host.target / "rmp-old.service").unlink()
    states = {**STATES, "rmp-b.timer": "enabled", "rmp-d.service": "enabled"}
    assert run(host, systemctl=FakeSystemctl(states)) == 0
    assert capsys.readouterr().out == f"head: {host.head}\nnothing to install\n"

    systemctl = FakeSystemctl(states)
    # An abbreviated id of the reviewed commit names it as well as the whole one.
    assert run(host, "--apply", "--head", host.head[:12], systemctl=systemctl) == 0
    assert [call for call in systemctl.calls if call[0] != "is-enabled"] == []
    [backup] = host.backups.iterdir()
    assert list(backup.iterdir()) == []
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == [f"head: {host.head}", "nothing to install", f"backup: {backup}"]
    assert out[3] == f"rollback: cp -a {backup}/. {host.target}/ && systemctl daemon-reload"


def test_a_masked_static_or_not_found_listed_unit_is_left_as_it_is(host, capsys):
    # The install changes rmp-a.service's file but does not add it, so not-found is no unit to enable but one for the
    # operator to diagnose.
    states = {**STATES, "rmp-a.service": "not-found", "rmp-c.service": "masked", "rmp-d.service": "static"}
    left = ["left as is: rmp-a.service (not-found)", "left as is: rmp-c.service (masked)",
            "left as is: rmp-d.service (static)"]
    assert run(host, systemctl=FakeSystemctl(states)) == 0
    assert [line for line in capsys.readouterr().out.splitlines() if line.startswith("left as is: ")] == left

    systemctl = FakeSystemctl(states, reloaded={"rmp-b.timer": "disabled"})
    assert run(host, "--apply", "--head", host.head, systemctl=systemctl) == 0
    assert [call for call in systemctl.calls if call[0] == "enable"] == [("enable", "rmp-b.timer")]
    assert [line for line in capsys.readouterr().out.splitlines() if line.startswith("left as is: ")] == left


def test_a_failing_daemon_reload_stops_before_any_enable(host, capsys):
    systemctl = FakeSystemctl(failing=[("daemon-reload",)])
    assert run(host, "--apply", "--head", host.head, systemctl=systemctl) == 1
    assert systemctl.calls[-1] == ("daemon-reload",) and not [call for call in systemctl.calls if call[0] == "enable"]
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == [f"head: {host.head}", "failed: systemctl daemon-reload (exit 1)", "installed: rmp-a.service, "
                       "rmp-b.service, rmp-b.timer, rmp-c.service.d/10-limits.conf, rmp-d.service.d/05-nice.conf"]
    [backup] = host.backups.iterdir()
    assert f"rollback: cp -a {backup}/. {host.target}/ && rm -f {host.target}/rmp-b.service" in "\n".join(out)
    assert (host.target / "rmp-b.timer").read_bytes() == committed(host, "rmp-b.timer")


def test_a_failing_enable_stops_there_and_its_rollback_disables_what_was_enabled(host, capsys):
    systemctl = FakeSystemctl(reloaded={"rmp-b.timer": "disabled"}, failing=[("enable", "rmp-d.service")])
    assert run(host, "--apply", "--head", host.head, systemctl=systemctl) == 1
    assert systemctl.calls[-2:] == [("enable", "rmp-b.timer"), ("enable", "rmp-d.service")]
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == [f"head: {host.head}", "failed: systemctl enable rmp-d.service (exit 1)"]
    assert "enabled: rmp-b.timer" in out and "start: rmp-b.timer" in out and "start: rmp-d.service" not in out
    assert next(line for line in out if line.startswith("rollback: ")).startswith(
        "rollback: systemctl disable rmp-b.timer && cp -a ")


def test_a_systemctl_timeout_stops_there_and_prints_the_rollback(host, capsys):
    before = snapshot(host.target)
    systemctl = FakeSystemctl(timing_out=[("daemon-reload",)])
    assert run(host, "--apply", "--head", host.head, systemctl=systemctl) == 1
    assert systemctl.calls[-1] == ("daemon-reload",)
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == [f"head: {host.head}", "failed: systemctl daemon-reload (timed out after 300 s)",
                       "installed: rmp-a.service, rmp-b.service, rmp-b.timer, rmp-c.service.d/10-limits.conf, "
                       "rmp-d.service.d/05-nice.conf"]
    roll_back(out)
    assert snapshot(host.target) == {**before, "rmp-c.service.d": ("dir", 0o755)}


def torn_backup(monkeypatch, host):
    """The disk fills while rmp-d.service.d/05-nice.conf is backed up, halfway through the copy."""
    copy2 = shutil.copy2

    def copy(src, dst, **kwargs):
        if Path(src) == host.target / "rmp-d.service.d" / "05-nice.conf":
            Path(dst).write_bytes(Path(src).read_bytes()[:4])
            raise OSError(errno.ENOSPC, "No space left on device")
        return copy2(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "copy2", copy)


def failed_write(monkeypatch, host):
    """rmp-b.timer's temp file is written, and putting it in place fails."""
    replace = os.replace

    def put(src, dst, **kwargs):
        if Path(dst) == host.target / "rmp-b.timer":
            raise OSError(errno.EIO, "Input/output error")
        return replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", put)


@pytest.mark.parametrize("fault, lines, rollback", [
    (torn_backup, ["failed: backing up rmp-d.service.d/05-nice.conf ([Errno 28] No space left on device)"],
     "cp -a {backup}/. {target}/ && systemctl daemon-reload"),
    (failed_write, ["failed: writing rmp-b.timer ([Errno 5] Input/output error)",
                    "installed: rmp-a.service, rmp-b.service"],
     "cp -a {backup}/. {target}/ && rm -f {target}/rmp-b.service && systemctl daemon-reload"),
], ids=["backing-up", "writing"])
def test_a_failure_while_backing_up_or_writing_prints_a_rollback_that_restores_every_file(host, capsys, monkeypatch,
                                                                                         fault, lines, rollback):
    before = snapshot(host.target)
    systemctl = FakeSystemctl()
    with monkeypatch.context() as patched:
        fault(patched, host)
        assert run(host, "--apply", "--head", host.head, systemctl=systemctl) == 1
    assert [call for call in systemctl.calls if call[0] != "is-enabled"] == []
    out = capsys.readouterr().out.splitlines()
    [backup] = host.backups.iterdir()
    assert out == [f"head: {host.head}", *lines, f"backup: {backup}",
                   f"rollback: {rollback.format(backup=backup, target=host.target)}",
                   "Each changed unit takes effect at its next start; restart one now only in an idle window. Start "
                   "each unit on a start: line in an idle window."]
    # The rollback puts back the file written before the failure, and copies no part-written backup or temp file.
    roll_back(out)
    assert snapshot(host.target) == before


def test_a_backup_directory_never_reuses_an_earlier_runs_stamp(host, capsys, monkeypatch):
    install_units = load()

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 17, 3, 4, 5, tzinfo=tz)

    monkeypatch.setattr(install_units, "datetime", Frozen)
    earlier = {}
    for name in ("20261017T030405Z", "20261017T030405Z-1"):
        (host.backups / name).mkdir(parents=True)
        (host.backups / name / "rmp-a.service").write_text(f"{name}'s backup\n")
        earlier[name] = snapshot(host.backups / name)
    assert run(host, "--apply", "--head", host.head, systemctl=FakeSystemctl(), module=install_units) == 0
    backup = host.backups / "20261017T030405Z-2"
    assert f"backup: {backup}" in capsys.readouterr().out.splitlines()
    assert (backup / "rmp-a.service").read_text() == unit("a")
    assert {name: snapshot(host.backups / name) for name in earlier} == earlier


def test_the_defaults_are_the_hosts_and_the_guard_denies_each():
    install_units = load()
    assert install_units.BACKUP_ROOT == Path("/var/backups/rmp-units")
    for path in (install_units.BACKUP_ROOT, deploy.UNIT_TARGET, deploy.CODE_RELOAD_LOCK):
        assert GUARD.denies(path), path
