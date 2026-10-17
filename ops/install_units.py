#!/usr/bin/env python3
r"""Install the unit files tracked in ops/systemd/ on this host: the operator's reviewed step after a deploy.

The deploy never installs a unit file; its result and Kirill's note name the changed ones. Run this as root from the
live checkout once the deploy shows ``deployed``. Without a flag it is a dry run that writes nothing. It prints the
commit it planned from on a head: line, then each new or changed unit file with the last commit that touched it, each
RMP unit file installed with no tracked file, each tracked unit file whose installed path is not a regular file (a
mask's link to /dev/null, for example) or lies in a <unit>.d/ that is a symlink or no directory, which it leaves as it
is, and each unit of enabled-units.txt that is not enabled or is left as it is:

    systemd-run --wait --pipe --collect --unit=rmp-install-units -p WorkingDirectory=/root/.openclaw/rmp \
      -p Environment=PYTHONPATH=/root/.openclaw/rmp -p EnvironmentFile=-/etc/rmp/rmp.env \
      -p EnvironmentFile=-/etc/openclaw/openclaw.env \
      /root/.openclaw/rmp/venv/bin/python /root/.openclaw/rmp/ops/install_units.py

Before --apply, review each new or changed file with

    diff -u /etc/systemd/system/<name> <(git show HEAD:ops/systemd/<name>)

(/dev/null for a new file's installed copy), and check the commit the dry run names. Install only changes that trace
to a reviewed programme PR. A unit change from any other source (Aura's PRs, a coding job) needs Kirill's explicit OK
first. --apply installs every new and changed file at once; <sha> is the full sha on the reviewed dry run's head: line:

    systemd-run --wait --pipe --collect --unit=rmp-install-units -p WorkingDirectory=/root/.openclaw/rmp \
      -p Environment=PYTHONPATH=/root/.openclaw/rmp -p EnvironmentFile=-/etc/rmp/rmp.env \
      -p EnvironmentFile=-/etc/openclaw/openclaw.env \
      /root/.openclaw/rmp/venv/bin/python /root/.openclaw/rmp/ops/install_units.py --apply --head <sha>

--apply refuses unless HEAD is still that commit, so a deploy that lands after the review installs nothing unreviewed.
It also refuses while a user task is active or a deploy or code reload holds the code-reload lock. It installs that
commit's bytes, never the working tree, backs up every file it replaces, never writes, backs up or removes anything
at an installed path that is not a regular file or through a <unit>.d/ that is not a real directory, runs one
daemon-reload and enables each listed unit that is not enabled. It never starts, restarts, disables or deletes a unit:
it prints a start: line for each unit it enabled, to start in an idle window, and a rollback: line that undoes the
install. A failure after the backup directory exists stops there, prints failed:, what was done so far and the
rollback: line, and exits 1.
"""
import argparse
import fcntl
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Collection, Dict, Iterator, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.coding import deploy  # noqa: E402

BACKUP_ROOT = Path("/var/backups/rmp-units")
ENABLED_LIST = "enabled-units.txt"
AFTERWARDS = ("Each changed unit takes effect at its next start; restart one now only in an idle window. Start each "
              "unit on a start: line in an idle window.")
USAGE = "usage: install_units.py [--apply --head <sha>], <sha> being the full sha on the reviewed dry run's head: line"
# A whole commit id, SHA-1 or SHA-256, compared with HEAD's as a string. A name such as HEAD or main, or an abbreviated
# id that a hex-named branch answers for, could follow a later deploy instead of naming the reviewed commit.
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# systemctl with its arguments: its exit code and its output.
Systemctl = Callable[..., Tuple[int, str]]


def _systemctl(*args: str) -> Tuple[int, str]:
    run = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=300)
    return run.returncode, run.stdout


def _active_user_tasks() -> int:
    from app.production.canary_sentinel import count_active_user_tasks_sync

    return count_active_user_tasks_sync(strict=True)


def _head(repo: Path) -> str:
    return deploy._git(repo, "rev-parse", "HEAD").strip()


class Plan:
    """What commit ``head`` asks of this host's units: the new and changed unit files, those whose installed path is
    not a regular file and stays as it is, the host-only ones, and the listed units that are not enabled or are left
    as they are."""

    def __init__(self, repo: Path, head: str, target: Path, systemctl: Systemctl):
        self.head = head
        drift = deploy.unit_drift(repo, self.head, target)
        # An installed path that is not a regular file, a mask's link to /dev/null for example, or a drop-in directory
        # linked elsewhere, is someone's deliberate choice: nothing is written, backed up or removed at it or through
        # it, so the install neither unmasks a unit, loses a link nor changes a file outside the target.
        self.kept = [name for name in drift["units"] if name.endswith(deploy.NOT_REGULAR)]
        units = [name for name in drift["units"] if name not in self.kept]
        self.files = [name.removesuffix(deploy.MISSING) for name in units]
        self.new = {name.removesuffix(deploy.MISSING) for name in units if name.endswith(deploy.MISSING)}
        self.host_only = drift["host_only_units"]
        listed = deploy._git(repo, "cat-file", "blob", f"{self.head}:{deploy.UNIT_DIR}{ENABLED_LIST}")
        lines = (line.strip() for line in listed.splitlines())
        self.listed = [line for line in lines if line and not line.startswith("#")]
        self.not_enabled, self.left = enablement(self.listed, self.new, systemctl)


def enablement(units: Sequence[str], new: Collection[str], systemctl: Systemctl) -> Tuple[List[str], List[str]]:
    """The units that are not enabled, and those left as they are, as "<unit> (<state>)". The state is the first word
    of ``systemctl is-enabled``, whatever its exit code. ``disabled`` is not enabled, and so is ``not-found`` for a unit
    whose file the install adds. Any other state, masked or static for example, is left as it is: a masked unit is
    someone's deliberate stop, and the operator diagnoses it."""
    not_enabled, left = [], []
    for unit in units:
        words = systemctl("is-enabled", unit)[1].split()
        state = words[0] if words else ""
        if state == "enabled":
            continue
        if state == "disabled" or (state == "not-found" and unit in new):
            not_enabled.append(unit)
        else:
            left.append(f"{unit} ({state or 'no state'})")
    return not_enabled, left


def dry_run(repo: Path, target: Path, systemctl: Systemctl) -> int:
    plan = Plan(repo, _head(repo), target, systemctl)
    print(f"head: {plan.head}")
    for name in plan.files:
        commit = deploy._git(repo, "log", "-1", "--format=%h %s", plan.head, "--", f"{deploy.UNIT_DIR}{name}").strip()
        print(f"{'new' if name in plan.new else 'changed'}: {name} ({commit})")
    if not plan.files:
        print("nothing to install")
    for name in plan.host_only:
        print(f"host-only: {name}")
    for unit in plan.not_enabled:
        print(f"not enabled: {unit}")
    for unit in plan.kept + plan.left:
        print(f"left as is: {unit}")
    return 0


def _directory_like(directory: Path, original: Path) -> None:
    """Create ``directory`` with ``original``'s permission bits, so that the rollback's ``cp -a`` leaves them as they
    are."""
    directory.mkdir()
    os.chmod(directory, stat.S_IMODE(original.stat().st_mode))


def _backup_directory(backup_root: Path, target: Path) -> Path:
    """A new directory for this run's backups, named by the UTC time, with ``-<n>`` added when that name is taken. The
    lock keeps a second run from taking it in between."""
    backup_root.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_root, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup, n = backup_root / stamp, 0
    while backup.exists():
        n += 1
        backup = backup_root / f"{stamp}-{n}"
    _directory_like(backup, target)
    return backup


@contextmanager
def _replacing(dest: Path) -> Iterator[Path]:
    """A temp file beside ``dest`` that replaces it once written. It is a new file under a name of its own, created
    with ``O_EXCL``, so nothing already there, a link left at a temp name for example, is written through or moved onto
    ``dest``. A failure leaves ``dest`` as it was and no temp file, so the rollback's ``cp -a`` never copies a
    part-written backup."""
    fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".rmp-install", dir=dest.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        yield tmp
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _succeeded(result: Tuple[int, str]) -> None:
    if result[0] != 0:
        raise RuntimeError(f"exit {result[0]}")


def apply(repo: Path, head: str, target: Path, backup_root: Path, systemctl: Systemctl) -> int:
    """Back up, install ``head``'s unit files, reload and enable; the caller holds the lock, checked that ``head`` is
    the reviewed commit and saw Aura idle. Once the backup directory exists, any failure (a systemctl call that exits
    non-zero, raises or times out, or an error while backing up or writing) stops the install: it prints ``failed:``,
    the lines for what was done and the rollback line, and makes no further systemctl call."""
    plan = Plan(repo, head, target, systemctl)
    print(f"head: {head}")
    backup = _backup_directory(backup_root, target)
    done: Dict[str, List[str]] = {"installed": [], "enabled": [], "left": plan.kept + plan.left, "disable": []}
    step = ""

    def call(*args: str) -> Tuple[int, str]:
        nonlocal step
        step = f"systemctl {' '.join(args)}"
        return systemctl(*args)

    failed = None
    try:
        for name in plan.files:
            if name in plan.new:
                continue
            step = f"backing up {name}"
            if not (backup / name).parent.is_dir():
                _directory_like((backup / name).parent, (target / name).parent)
            with _replacing(backup / name) as tmp:
                shutil.copy2(target / name, tmp)
        for name in plan.files:
            step = f"writing {name}"
            dest = target / name
            if not dest.parent.is_dir():
                dest.parent.mkdir()
                os.chmod(dest.parent, 0o755)
            with _replacing(dest) as tmp:
                tmp.write_bytes(deploy._git_bytes(repo, "cat-file", "blob", f"{plan.head}:{deploy.UNIT_DIR}{name}"))
                os.chmod(tmp, 0o644)
            done["installed"].append(name)
        if done["installed"]:
            _succeeded(call("daemon-reload"))
        not_enabled, left = enablement(plan.listed, plan.new, call)
        done["left"] = plan.kept + left
        for unit in not_enabled:
            # An enable that exits non-zero or times out may have enabled its unit all the same, so the rollback
            # disables it too; disabling a unit that is not enabled changes nothing.
            done["disable"].append(unit)
            _succeeded(call("enable", unit))
            done["enabled"].append(unit)
    except Exception as exc:
        reason = (f"timed out after {exc.timeout:g} s" if isinstance(exc, subprocess.TimeoutExpired)
                  else str(exc) or type(exc).__name__)
        failed = f"{step} ({reason})"
    if failed:
        print(f"failed: {failed}")
    _report(done, plan, backup, target)
    return 1 if failed else 0


def _report(done: Dict[str, List[str]], plan: Plan, backup: Path, target: Path) -> None:
    if done["installed"]:
        print(f"installed: {', '.join(done['installed'])}")
    elif not plan.files:
        print("nothing to install")
    if done["enabled"]:
        print(f"enabled: {', '.join(done['enabled'])}")
    for unit in done["enabled"]:
        print(f"start: {unit}")
    for unit in done["left"]:
        print(f"left as is: {unit}")
    print(f"backup: {backup}")
    steps = [f"systemctl disable {' '.join(done['disable'])}"] if done["disable"] else []
    steps.append(f"cp -a {backup}/. {target}/")
    added = [str(target / name) for name in done["installed"] if name in plan.new]
    if added:
        steps.append(f"rm -f {' '.join(added)}")
    steps.append("systemctl daemon-reload")
    print(f"rollback: {' && '.join(steps)}")
    print(AFTERWARDS)


def main(argv: Optional[Sequence[str]] = None, *, repo: Optional[Path] = None, target: Optional[Path] = None,
         backup_root: Optional[Path] = None, lock: Optional[Path] = None, systemctl: Optional[Systemctl] = None,
         active_user_tasks: Optional[Callable[[], int]] = None) -> int:
    parser = argparse.ArgumentParser(description="Install the unit files tracked in ops/systemd/; a dry run unless "
                                                 "--apply is given.")
    parser.add_argument("--apply", action="store_true",
                        help="install the new and changed files, back up those replaced, daemon-reload and enable")
    parser.add_argument("--head", metavar="SHA",
                        help="the full sha on the reviewed dry run's head: line; --apply needs it and installs only it")
    args = parser.parse_args(argv)
    repo = repo or REPO
    target = target or deploy.UNIT_TARGET
    systemctl = systemctl or _systemctl
    if not args.apply:
        return dry_run(repo, target, systemctl)
    if not SHA.fullmatch(args.head or ""):
        print(USAGE)
        return 2
    lock = lock or deploy.CODE_RELOAD_LOCK
    with open(lock, "w") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"refused: a deploy or code reload holds {lock}")
            return 2
        # Under the lock, so no deploy moves HEAD between this check and the install.
        head = _head(repo)
        if args.head != head:
            print(f"refused: HEAD is {head}, the reviewed dry run was {args.head}")
            return 2
        try:
            busy = (active_user_tasks or _active_user_tasks)()
        except Exception as exc:
            print(f"refused: could not count active user tasks ({exc})")
            return 2
        if busy > 0:
            print(f"refused: {busy} user task(s) active; run again when Aura is idle")
            return 2
        return apply(repo, head, target, backup_root or BACKUP_ROOT, systemctl)


if __name__ == "__main__":
    sys.exit(main())
