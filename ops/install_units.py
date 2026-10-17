#!/usr/bin/env python3
r"""Install the unit files tracked in ops/systemd/ on this host: the operator's reviewed step after a deploy.

The deploy never installs a unit file; its result and Kirill's note name the changed ones. Run this as root from the
live checkout once the deploy shows ``deployed``. Without a flag it is a dry run that writes nothing. It prints each
new or changed unit file with the last commit that touched it, each RMP unit file installed with no tracked file, and
each unit of enabled-units.txt that is not enabled or is left as it is:

    systemd-run --wait --pipe --collect --unit=rmp-install-units -p WorkingDirectory=/root/.openclaw/rmp \
      -p Environment=PYTHONPATH=/root/.openclaw/rmp -p EnvironmentFile=-/etc/rmp/rmp.env \
      -p EnvironmentFile=-/etc/openclaw/openclaw.env \
      /root/.openclaw/rmp/venv/bin/python /root/.openclaw/rmp/ops/install_units.py

Before --apply, review each new or changed file with

    diff -u /etc/systemd/system/<name> <(git show HEAD:ops/systemd/<name>)

(/dev/null for a new file's installed copy), and check the commit the dry run names. Install only changes that trace
to a reviewed programme PR. A unit change from any other source (Aura's PRs, a coding job) needs Kirill's explicit OK
first. --apply installs every new and changed file at once:

    systemd-run --wait --pipe --collect --unit=rmp-install-units -p WorkingDirectory=/root/.openclaw/rmp \
      -p Environment=PYTHONPATH=/root/.openclaw/rmp -p EnvironmentFile=-/etc/rmp/rmp.env \
      -p EnvironmentFile=-/etc/openclaw/openclaw.env \
      /root/.openclaw/rmp/venv/bin/python /root/.openclaw/rmp/ops/install_units.py --apply

--apply refuses while a user task is active or a deploy or code reload holds the code-reload lock. It installs HEAD's
committed bytes, never the working tree, backs up every file it replaces, runs one daemon-reload and enables each
listed unit that is not enabled. It never starts, restarts, disables or deletes a unit: it prints a start: line for
each unit it enabled, to start in an idle window, and a rollback: line that undoes the install.
"""
import argparse
import fcntl
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Collection, Dict, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.coding import deploy  # noqa: E402

BACKUP_ROOT = Path("/var/backups/rmp-units")
ENABLED_LIST = "enabled-units.txt"
AFTERWARDS = ("Each changed unit takes effect at its next start; restart one now only in an idle window. Start each "
              "unit on a start: line in an idle window.")

# systemctl with its arguments: its exit code and its output.
Systemctl = Callable[..., Tuple[int, str]]


def _systemctl(*args: str) -> Tuple[int, str]:
    run = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=300)
    return run.returncode, run.stdout


def _active_user_tasks() -> int:
    from app.production.canary_sentinel import count_active_user_tasks_sync

    return count_active_user_tasks_sync(strict=True)


class Plan:
    """What HEAD asks of this host's units: the new and changed unit files, the host-only ones, and the listed units
    that are not enabled or are left as they are."""

    def __init__(self, repo: Path, target: Path, systemctl: Systemctl):
        self.head = deploy._git(repo, "rev-parse", "HEAD").strip()
        drift = deploy.unit_drift(repo, self.head, target)
        self.files = [name.removesuffix(deploy.MISSING) for name in drift["units"]]
        self.new = {name.removesuffix(deploy.MISSING) for name in drift["units"] if name.endswith(deploy.MISSING)}
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
    plan = Plan(repo, target, systemctl)
    for name in plan.files:
        commit = deploy._git(repo, "log", "-1", "--format=%h %s", plan.head, "--", f"{deploy.UNIT_DIR}{name}").strip()
        print(f"{'new' if name in plan.new else 'changed'}: {name} ({commit})")
    if not plan.files:
        print("nothing to install")
    for name in plan.host_only:
        print(f"host-only: {name}")
    for unit in plan.not_enabled:
        print(f"not enabled: {unit}")
    for unit in plan.left:
        print(f"left as is: {unit}")
    return 0


def _directory_like(directory: Path, original: Path) -> None:
    """Create ``directory`` with ``original``'s permission bits, so that the rollback's ``cp -a`` leaves them as they
    are."""
    directory.mkdir()
    os.chmod(directory, stat.S_IMODE(original.stat().st_mode))


def apply(repo: Path, target: Path, backup_root: Path, systemctl: Systemctl) -> int:
    """Back up, install, reload and enable; the caller holds the lock and saw Aura idle."""
    plan = Plan(repo, target, systemctl)
    backup_root.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_root, 0o700)
    backup = backup_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _directory_like(backup, target)
    for name in plan.files:
        if name in plan.new:
            continue
        if not (backup / name).parent.is_dir():
            _directory_like((backup / name).parent, (target / name).parent)
        shutil.copy2(target / name, backup / name)
    done: Dict[str, List[str]] = {"installed": [], "enabled": [], "left": plan.left}
    for name in plan.files:
        dest = target / name
        if not dest.parent.is_dir():
            dest.parent.mkdir()
            os.chmod(dest.parent, 0o755)
        tmp = dest.with_name(f".{dest.name}.rmp-install")
        tmp.write_bytes(deploy._git_bytes(repo, "cat-file", "blob", f"{plan.head}:{deploy.UNIT_DIR}{name}"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
        done["installed"].append(name)
    failed = None
    if done["installed"]:
        code, _ = systemctl("daemon-reload")
        if code != 0:
            failed = f"daemon-reload (exit {code})"
    if failed is None:
        not_enabled, done["left"] = enablement(plan.listed, plan.new, systemctl)
        for unit in not_enabled:
            code, _ = systemctl("enable", unit)
            if code != 0:
                failed = f"enable {unit} (exit {code})"
                break
            done["enabled"].append(unit)
    if failed:
        print(f"failed: systemctl {failed}")
    _report(done, plan.new, backup, target)
    return 1 if failed else 0


def _report(done: Dict[str, List[str]], new: Collection[str], backup: Path, target: Path) -> None:
    print(f"installed: {', '.join(done['installed'])}" if done["installed"] else "nothing to install")
    if done["enabled"]:
        print(f"enabled: {', '.join(done['enabled'])}")
    for unit in done["enabled"]:
        print(f"start: {unit}")
    for unit in done["left"]:
        print(f"left as is: {unit}")
    print(f"backup: {backup}")
    steps = [f"systemctl disable {' '.join(done['enabled'])}"] if done["enabled"] else []
    steps.append(f"cp -a {backup}/. {target}/")
    added = [str(target / name) for name in done["installed"] if name in new]
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
    args = parser.parse_args(argv)
    repo = repo or REPO
    target = target or deploy.UNIT_TARGET
    systemctl = systemctl or _systemctl
    if not args.apply:
        return dry_run(repo, target, systemctl)
    lock = lock or deploy.CODE_RELOAD_LOCK
    with open(lock, "w") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"refused: a deploy or code reload holds {lock}")
            return 2
        try:
            busy = (active_user_tasks or _active_user_tasks)()
        except Exception as exc:
            print(f"refused: could not count active user tasks ({exc})")
            return 2
        if busy > 0:
            print(f"refused: {busy} user task(s) active; run again when Aura is idle")
            return 2
        return apply(repo, target, backup_root or BACKUP_ROOT, systemctl)


if __name__ == "__main__":
    sys.exit(main())
