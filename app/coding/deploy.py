"""Shipping Aura's code: GitHub's main is protected, so every change lands there through a pull request first.

Two changes ship this way. Aura's own pull requests: Claude merges one once she approves and CI passed, and
``watch_main`` queues the deploy of GitHub's main once CI passed on that commit too. A reviewed coding
job's approved commit: the worker fetches it into the live repo, checks it fast-forwards ``main`` and runs
the full suite on that exact commit as aura-coder, then hands off; the unit lands it through a pull request.

A deploy waits queued on disk (``RUNS_DIR/<task>``). One launcher, ``next_deploy``, run by the reconciler's
``watch_main`` every minute and by the hand-off, starts the detached root unit (``ops/coding_deploy.py``) of the
next queued run, only when no deploy unit runs, nothing holds the code-reload lock and no user task is active.
The unit does its slow work first (a reviewed run's land and CI wait), then holds the code-reload lock, refuses
mirrors edited by hand and, right after the strict idle count, stops the affected units, the API first with a
second count; it fast-forwards the live ``main`` to GitHub's, reconciles the mirrors in full and starts the units
again, then checks health, readiness and a canary. It never installs a unit file; the result names the changed
ones. On failure it reverts the live ``main`` the same way at once; the revert reaches GitHub as a pull request of
its own. Each phase is written to the run's state as it happens, so a killed unit's run resumes from its phase. It
records the result, then a reviewed job's workflow replies on the new code, or Kirill gets a note for Aura's own.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from app.coding import github, verify, workspace
from app.coding.units import CODE_ROOT, CODER_USER, JOBS_DIR, RUNS_DIR
from app.config import OPENCLAW_HOME

logger = logging.getLogger(__name__)

LIVE_REPO = Path(OPENCLAW_HOME) / "rmp"
BUNDLES_DIR = CODE_ROOT / "bundles"
CODE_RELOAD_LOCK = Path("/run/rmp-code-reload.lock")
# How long a run waits before a launcher pass that finds Aura busy launches it to record ``postponed``.
IDLE_WAIT_SEC = 2 * 3600
# How long a deploy unit waits for the code-reload lock before it ends, leaving its run queued, and how often it tries.
LOCK_WAIT_SEC = 300
LOCK_POLL_SEC = 5
DEPLOY_UNIT_MAX_SEC = 3 * 3600
# The unit starts the change only while this much of its time is left: the window, the checks and a rollback.
APPLY_RESERVE_SEC = 90 * 60
# The run's phases from ``applying`` on: the host may be mid-change, so such a run is resumed, never started over.
CHANGING = ("applying", "merged", "restarted", "reverting")
# Errors before the change a run survives: the last of them ends it ``failed``.
MAX_ATTEMPTS = 3
# (path or directory prefix, services restarted when it changes); docs, tests and ops restart nothing.
SERVICE_PATHS = (
    ("app/", ("rmp-api", "rmp-worker")),
    ("worker.py", ("rmp-api", "rmp-worker")),
    ("requirements.txt", ("rmp-api", "rmp-worker")),
    ("plugins/", ("openclaw-gateway",)),
    ("web-stack/", ("aura-web-backends",)),
)
# Repo directories with a live copy elsewhere on the host.
PLUGINS_MIRROR = Path(OPENCLAW_HOME) / "plugins"
WEB_STACK_MIRROR = Path(OPENCLAW_HOME) / "web-stack"
CURSOR_RULES_MIRROR = Path("/root/.cursor/rules")
MIRRORS = (
    ("plugins/", PLUGINS_MIRROR),
    ("web-stack/", WEB_STACK_MIRROR),
    (".cursor/rules/", CURSOR_RULES_MIRROR),
)
# The tracked unit files and where systemd reads them. No deploy writes there: an operator installs them.
UNIT_DIR = "ops/systemd/"
UNIT_TARGET = Path("/etc/systemd/system")
UNIT_SUFFIXES = (".service", ".timer", ".path", ".socket")
# The units of this host that are RMP's, by name; an installed one the repo does not track is host-only.
RMP_UNIT_PREFIXES = ("rmp-", "aura-", "openclaw-", "temporal.", "temporal-")
# Marks a tracked unit file with no installed copy, and one whose installed path is not a regular file: a symlink, such
# as a mask's link to /dev/null, anything else that no install writes over, or a path in a <unit>.d/ that is a symlink
# or no directory.
MISSING = " (missing)"
NOT_REGULAR = " (not a regular file)"


class Interrupted(BaseException):
    """The deploy unit was told to stop (``SIGTERM``). A ``BaseException``, so no ``except Exception`` of the deploy
    swallows it: it passes through the switch window, which starts the units it stopped, to the unit's ``main``."""


def restarts_for(paths: Iterable[str]) -> List[str]:
    """The services a deploy restarts: the API and worker for app code, the gateway for plugins, the web backends for
    the web stack. A unit file restarts nothing: the deploy never installs one."""
    services = set()
    for path in paths:
        for prefix, units in SERVICE_PATHS:
            if path == prefix or (prefix.endswith("/") and path.startswith(prefix)):
                services.update(units)
    return sorted(services)


def _git(repo: Path, *args: str, env: Optional[Dict[str, str]] = None) -> str:
    return workspace.git(*args, cwd=repo, env=env)


def _git_bytes(repo: Path, *args: str, stdin: bytes = b"") -> bytes:
    """``_git`` with bytes in and out; it fails as ``workspace.git`` does, after 900 s at most and with git's reason."""
    run = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo, input=stdin, capture_output=True,
                         timeout=900)
    if run.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {run.stderr.decode(errors='replace').strip()[-400:]}")
    return run.stdout


def changes(repo: Path, old: str, new: str) -> List[Tuple[str, str]]:
    out = _git(repo, "diff", "--name-status", "--no-renames", old, new)
    return [tuple(line.split("\t", 1)) for line in out.splitlines()]


def tracked_files(repo: Path, commit: str, prefix: str) -> List[Tuple[str, str, str]]:
    """Every file under ``prefix`` at ``commit`` as (mode, blob, path), from one ``ls-tree``."""
    files = []
    for entry in _git(repo, "ls-tree", "-r", "-z", commit, "--", prefix).split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            mode, _, blob = meta.split()
            files.append((mode, blob, path))
    return files


def blob_ids(repo: Path, files: List[Path]) -> List[str]:
    """The blob id of each file's bytes as they are (no filters), from one ``hash-object``."""
    if not files:
        return []
    return _git_bytes(repo, "hash-object", "--no-filters", "--stdin-paths",
                      stdin=b"".join(os.fsencode(path) + b"\n" for path in files)).decode().split()


def drifted(repo: Path, old: str, prefix: str, target: Path) -> List[str]:
    """Tracked files of a mirror whose live copy matches no committed version: someone edited the live copy.

    A live copy that is missing or merely behind (an earlier committed version) is not drift; the reconcile writes it.
    """
    files = [(blob, path, target / path[len(prefix):]) for _, blob, path in tracked_files(repo, old, prefix)]
    present = [(blob, path, live) for blob, path, live in files if live.is_file()]
    found = []
    for (blob, path, _), now in zip(present, blob_ids(repo, [live for _, _, live in present])):
        if now != blob and now not in _git(repo, "log", "--format=", "--raw", "--no-abbrev", old, "--", path).split():
            found.append(path)
    return found


def _live_mode(mode: str) -> int:
    """The permission bits of a live copy of a file committed with ``mode``."""
    return 0o755 if mode == "100755" else 0o644


def _out_of_date(repo: Path, commit: str, prefix: str, target: Path) -> List[Tuple[str, str, str, Path]]:
    """Tracked files under ``prefix`` at ``commit`` whose live copy under ``target`` is missing or has other bytes or
    permission bits, as (mode, blob, path, live copy)."""
    files = [(mode, blob, path, target / path[len(prefix):])
             for mode, blob, path in tracked_files(repo, commit, prefix)]
    present = [dest for _, _, _, dest in files if dest.is_file()]
    current = dict(zip(present, blob_ids(repo, present)))
    return [(mode, blob, path, dest) for mode, blob, path, dest in files
            if current.get(dest) != blob or stat.S_IMODE(dest.stat().st_mode) != _live_mode(mode)]


def reconcile(repo: Path, commit: str, changed: List[Tuple[str, str]], prefix: str, target: Path) -> List[str]:
    """Make the mirror of ``prefix`` match ``commit``: delete the live copy of each file the range deleted, then write
    every tracked file whose live copy is missing or has other bytes or permission bits. Files the commit does not track
    are never touched. The repo paths deleted or written."""
    done = []
    for status, path in changed:
        if status == "D" and path.startswith(prefix):
            (target / path[len(prefix):]).unlink(missing_ok=True)
            done.append(path)
    for mode, blob, path, dest in _out_of_date(repo, commit, prefix, target):
        data = _git_bytes(repo, "cat-file", "blob", blob)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.aura-deploy")
        tmp.write_bytes(data)
        os.chmod(tmp, _live_mode(mode))
        os.replace(tmp, dest)
        done.append(path)
    return done


def is_unit_file(name: str) -> bool:
    """Whether ``name``, relative to a unit directory, is a unit file: a unit, or a ``.conf`` drop-in in a unit's
    ``<unit>.d/`` directory."""
    parts = name.split("/")
    if len(parts) == 1:
        return name.endswith(UNIT_SUFFIXES)
    return (len(parts) == 2 and parts[0].endswith(tuple(f"{suffix}.d" for suffix in UNIT_SUFFIXES))
            and parts[1].endswith(".conf"))


def _installed_rmp_unit_files(target: Path) -> List[str]:
    """RMP's unit files installed in ``target``, by name relative to it: its units' regular files there, and the
    ``.conf`` files in their ``<unit>.d/`` directories. Symlinks, ``*.wants/`` links and other units are not."""
    names = []
    with os.scandir(target) as entries:
        for entry in entries:
            if entry.is_symlink() or not entry.name.startswith(RMP_UNIT_PREFIXES):
                continue
            if entry.is_file():
                names.append(entry.name)
            elif entry.is_dir():
                with os.scandir(entry.path) as inside:
                    names += [f"{entry.name}/{item.name}" for item in inside if item.is_file(follow_symlinks=False)]
    return sorted(name for name in names if is_unit_file(name))


def _not_regular(path: Path, target: Path) -> bool:
    """Whether the installed path ``path`` under ``target`` is there but is not a regular file, or lies in a
    ``<unit>.d/`` that is there but is a symlink or no directory. Nothing is read or written through such a path."""
    directory = path.parent
    if directory != target and os.path.lexists(directory) and (directory.is_symlink() or not directory.is_dir()):
        return True
    return os.path.lexists(path) and (path.is_symlink() or not path.is_file())


def unit_drift(repo: Path, commit: str, target: Path) -> Dict[str, List[str]]:
    """How the unit files installed in ``target`` differ from ``commit``'s, by name relative to both: ``units``, the
    tracked unit files whose installed copy is missing (marked ``MISSING``), is not a regular file or lies in a
    ``<unit>.d/`` that is not a real directory (marked ``NOT_REGULAR``, and never read through a symlink) or has other
    bytes; ``host_only_units``, RMP's unit files installed there that ``commit`` does not track."""
    tracked = {path[len(UNIT_DIR):]: blob for _, blob, path in tracked_files(repo, commit, UNIT_DIR)
               if is_unit_file(path[len(UNIT_DIR):])}
    names = sorted(tracked)
    other = {name for name in names if _not_regular(target / name, target)}
    present = [name for name in names if name not in other and (target / name).is_file()]
    current = dict(zip(present, blob_ids(repo, [target / name for name in present])))
    units = [f"{name}{NOT_REGULAR}" if name in other else name if name in current else f"{name}{MISSING}"
             for name in names if current.get(name) != tracked[name]]
    host_only = [name for name in _installed_rmp_unit_files(target) if name not in tracked]
    return {"units": units, "host_only_units": host_only}


def host_file_drift(repo: Path, commit: str = "HEAD") -> Dict[str, List[str]]:
    """What this host's files differ from ``commit`` in, by name only: ``unit_drift`` under ``UNIT_TARGET``, and
    ``mirrors``, the tracked mirror files whose live copy is missing or has other bytes or permission bits. The next
    deploy writes the mirror files; ``ops/install_units.py`` installs the unit files."""
    drift = unit_drift(repo, commit, UNIT_TARGET)
    drift["mirrors"] = [path for prefix, target in MIRRORS
                        for _, _, path, _ in _out_of_date(repo, commit, prefix, target)]
    return drift


def fetch_approved(job: workspace.Job, head: str, *, live: Path = LIVE_REPO) -> Dict[str, Any]:
    """Bring the approved commit into the live repo; ``needs_rebase`` when it no longer fast-forwards ``main``."""
    _git(live, "fetch", "--quiet", "--no-tags", job.review, f"+{head}:refs/aura/{job.task_id}")
    main = _git(live, "rev-parse", "refs/heads/main").strip()
    try:
        _git(live, "merge-base", "--is-ancestor", main, head)
    except RuntimeError:
        return {"status": "needs_rebase", "main": main}
    return {"status": "ready", "old": main}


def refresh_base(job: workspace.Job, *, live: Path = LIVE_REPO) -> workspace.Job:
    """Give a job today's ``main`` to rebase onto: a bundle aura-coder can read, and the review repository's new base.

    The bundle lives in a root-owned directory, never in the checkout, where a planted symlink could redirect the write.
    """
    main = _git(live, "rev-parse", "refs/heads/main").strip()
    folder = BUNDLES_DIR / job.task_id
    folder.mkdir(parents=True, exist_ok=True)
    for path in (BUNDLES_DIR, folder):
        os.chmod(path, 0o755)
    bundle = folder / "main.bundle"
    _git(live, "bundle", "create", "-q", str(bundle), "refs/heads/main")
    os.chmod(bundle, 0o644)
    workspace.git("--git-dir", job.review, "fetch", "--quiet", "--no-tags", str(live), "+refs/heads/main:refs/aura/base")
    updated = workspace.Job(**{**asdict(job), "base": main})
    workspace.job_file(job.task_id).write_text(json.dumps(asdict(updated), indent=2) + "\n")
    return updated


def exact_commit_suite(job: workspace.Job, head: str, cfg: Dict[str, Any], *, owner: Optional[str] = CODER_USER) -> Dict[str, Any]:
    """The full suite on a clean checkout of exactly the approved commit, as aura-coder in hardened units."""
    checkout = JOBS_DIR / f"{job.task_id}-deploy"
    shutil.rmtree(checkout, ignore_errors=True)
    workspace.git("clone", "--quiet", "--no-hardlinks", "--no-checkout", job.review, str(checkout))
    workspace.git("checkout", "--quiet", "--detach", head, cwd=checkout)
    (checkout / ".aura").mkdir()
    if owner:
        workspace._chown_tree(checkout, owner)
    os.chmod(checkout, 0o700)
    exact = workspace.Job(**{**asdict(job), "checkout": str(checkout)})
    try:
        return verify.run_tests(exact, cfg, attempt="deploy")
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def spec_file(task_id: str) -> Path:
    return RUNS_DIR / task_id / "deploy.json"


def state_file(task_id: str) -> Path:
    """The run's state. While it exists the run has work to do; the unit removes it once it records the result."""
    return RUNS_DIR / task_id / "deploy-state.json"


def stopped_file(task_id: str) -> Path:
    """The units a switch window holds stopped, one name per line; it exists only while they are stopped."""
    return RUNS_DIR / task_id / "stopped-units"


def stop_post_command(run_dir: Path) -> str:
    """The deploy unit's ``ExecStopPost=``, run after every end of the unit, a kill included: start the units its
    window listed as stopped. The run's literal path (no ``$``, which systemd would expand) and ``systemctl`` by bare
    name; after a normal end the list is gone and it does nothing, and it never fails the unit."""
    listed = run_dir / "stopped-units"
    return f"/bin/sh -c 'test -s {listed} && xargs -r systemctl start --no-block < {listed}; exit 0'"


def run_exists(task_id: str) -> bool:
    """Whether the task's deploy run has work to do: queued, waiting, or killed and not yet resumed."""
    return state_file(task_id).exists()


def write_json(path: Path, data: Dict[str, Any]) -> None:
    """Replace ``path`` atomically: a temporary file in its directory, flushed to disk, then renamed over it."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(json.dumps(data, indent=2) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def record_attempt(task_id: str, error: str) -> Optional[Dict[str, Any]]:
    """Count an error before the change (an exception or ``SIGTERM`` before ``applying``, when nothing on the host has
    changed): the attempt and its error go into the run's state, quietly, and the launcher relaunches the run through
    the gate. The ``MAX_ATTEMPTS``-th ends the run: its ``failed`` result. None while the run is retried, or when it has
    no state."""
    path = state_file(task_id)
    run = _read_json(path)
    if run is None:
        return None
    attempt = run.get("attempt", 0) + 1
    write_json(path, {**run, "attempt": attempt, "last_error": error[:300]})
    if attempt < MAX_ATTEMPTS:
        return None
    summary = f"The deploy failed {attempt} times before changing anything (last: {error[:300]}); main is unchanged."
    if run.get("phase") == "landed":
        summary += " GitHub's main has the change; it goes live with GitHub's main."
    return {"status": "failed", "summary": summary}


def queue(spec: Dict[str, Any]) -> str:
    """Queue a deploy run, its spec and its state ``queued``, for the launcher; the run's unit name.

    A spec whose head is stored already is not written again, so a retried activity does not deploy twice; for
    GitHub's main, which the watcher queues again after a ``postponed`` result, that holds while its run is queued. A
    different head replaces a run still queued; a run past that is kept, and the call raises.
    """
    task = spec["task_id"]
    path, state = spec_file(task), state_file(task)
    stored, run = _read_json(path), _read_json(state)
    if stored and stored.get("head") == spec["head"] and (run is not None or spec.get("source") != "github"):
        return f"aura-deploy-{task}"
    if run is not None and run.get("phase") != "queued":
        raise RuntimeError(f"a deploy of {task} is in progress")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(path, spec)
    write_json(state, {"phase": "queued", "queued_at": datetime.now(timezone.utc).isoformat(), "attempt": 0,
                       "launches": 0, "started": 0, "escapes": 0})
    return f"aura-deploy-{task}"


def mark_started(task_id: str) -> bool:
    """The unit checks in, first thing on every launch: the run's ``started`` becomes its ``launches``. False when
    the run has no state: it was ended, or aborted by hand."""
    path = state_file(task_id)
    run = _read_json(path)
    if run is None:
        return False
    write_json(path, {**run, "started": run.get("launches", 0)})
    return True


def _dead(run: Dict[str, Any]) -> bool:
    """Whether the run's units keep dying: ``MAX_ATTEMPTS`` launches never checked in, or ``MAX_ATTEMPTS`` errors
    escaped a phase from ``applying`` on. Only once no deploy unit is active: until a launched unit has ended, a
    launch not checked in may still check in. A state from before ``started`` (batch 1's) counts every launch as
    checked in."""
    launches = run.get("launches", 0)
    return launches - run.get("started", launches) >= MAX_ATTEMPTS or run.get("escapes", 0) >= MAX_ATTEMPTS


def queued_runs() -> List[Tuple[str, Dict[str, Any]]]:
    """The runs with work to do, as (task, state), in launch order: runs past ``landed``, to resume, then reviewed
    runs by when they were queued, then GitHub's main."""
    runs = []
    for path in RUNS_DIR.glob("*/deploy-state.json"):
        run = _read_json(path)
        if run is not None:
            runs.append((path.parent.name, run))
    return sorted(runs, key=lambda item: (item[1].get("phase") not in CHANGING, item[0] == MAIN,
                                          item[1].get("queued_at", "")))


def gate(host: "Host") -> Optional[Tuple[str, bool]]:
    """Why no deploy unit may start now and whether Aura is busy, or None when the gate is open: no deploy unit runs,
    nothing holds the code-reload lock and no user task is active, by the strict count. Only active user tasks make
    Aura busy; a count that fails closes the gate too, but usually means the database is down."""
    if host.deploy_units():
        return "a deploy unit is running", False
    if host.reload_lock_held():
        return "the code-reload lock is held", False
    try:
        active = host.active_user_tasks()
    except Exception as exc:
        return f"the idle count failed: {exc}", False
    if active > 0:
        return f"{active} active user task(s)", True
    return None


def launch(task: str, *, postpone: bool = False, live: Path) -> str:
    """Start the run's detached root unit, which outlives worker restarts, including its own; the unit started.

    The launch is counted in the run's state first; a failed start puts the state back, so the run stays queued. A
    run keeps one log: its first launch starts it empty, and every later launch of the run appends to it. Whatever
    ends the unit, its ``ExecStopPost=`` starts the units its window left stopped (``stop_post_command``).
    """
    path, state = spec_file(task), state_file(task)
    run = json.loads(state.read_text())
    unit = f"aura-deploy-{task}"
    log = f"{'append' if run.get('launches') else 'truncate'}:{path.with_name('deploy.log')}"
    props = [f"RuntimeMaxSec={DEPLOY_UNIT_MAX_SEC}", f"StandardOutput={log}", "StandardError=inherit",
             f"Environment=PYTHONPATH={live}", "EnvironmentFile=-/etc/rmp/rmp.env",
             "EnvironmentFile=-/etc/openclaw/openclaw.env", f"ExecStopPost={stop_post_command(path.parent)}"]
    argv = ["systemd-run", f"--unit={unit}", f"--working-directory={live}", "--collect", "--quiet",
            *[f"--property={prop}" for prop in props], "--",
            str(live / "venv" / "bin" / "python"), str(live / "ops" / "coding_deploy.py"), str(path),
            *(["--postpone"] if postpone else [])]
    # A state from before ``started`` gets it here, in the write that counts the launch, so its later launches count.
    write_json(state, {**run, "launches": run.get("launches", 0) + 1,
                       "started": run.get("started", run.get("launches", 0))})
    try:
        subprocess.run(argv, check=True, capture_output=True, timeout=60)
    except Exception:
        write_json(state, run)
        raise
    return unit


def next_deploy(host: "Host") -> Optional[str]:
    """The launcher: start the unit of the first queued run when the gate is open; the unit started, or None.

    It runs under ``RUNS_DIR/.launch.lock``, so the worker's hand-off and the API's reconciler never launch two units
    at once: when the other holds it, the reconciler's next pass launches. Dead runs (``_dead``) are skipped, and a
    dead run in a phase from ``applying`` on launches nothing at all: the host may be mid-change. A run still
    ``queued`` or ``landed`` ``IDLE_WAIT_SEC`` after it was queued is launched to record ``postponed``, only when Aura
    is busy; one waiting behind another deploy unit, the code-reload lock or a failed count waits on. A host without
    ``RUNS_DIR`` yet has no queued run: None.
    """
    if not RUNS_DIR.is_dir():
        return None
    fd = os.open(RUNS_DIR / ".launch.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        if host.deploy_units():
            return None
        runs = queued_runs()
        stuck = [task for task, run in runs if _dead(run) and run.get("phase") in CHANGING]
        if stuck:
            logger.debug("Nothing deploys while the run of %s is dead mid-change", stuck[0])
            return None
        runs = [(task, run) for task, run in runs if not _dead(run)]
        if not runs:
            return None
        task, run = runs[0]
        closed = gate(host)
        if closed is None:
            return launch(task, live=host.live)
        reason, busy = closed
        waited = datetime.now(timezone.utc) - datetime.fromisoformat(run["queued_at"])
        if busy and run["phase"] in ("queued", "landed") and waited.total_seconds() >= IDLE_WAIT_SEC:
            return launch(task, postpone=True, live=host.live)
        logger.debug("The deploy of %s waits: %s", task, reason)
        return None
    finally:
        os.close(fd)


def end_dead_runs(host: "Host") -> List[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """End the dead runs, under the launch lock and only while no deploy unit is active; (task, spec, result) for each
    to report, nothing when the lock is held or a unit runs.

    A dead run still ``queued`` or ``landed`` changed nothing: it ends ``failed``, with ``result.json`` written and its
    state removed. One in a phase from ``applying`` on may have left the host mid-change, and its state holds the only
    record of ``old``: it is kept, with ``dead_noted``, and reported ``stuck`` once; nothing else deploys until it is
    resumed by hand (specs/WP-09.md §0), and whoever does that removes its state."""
    if not RUNS_DIR.is_dir():
        return []
    fd = os.open(RUNS_DIR / ".launch.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return []
        if host.deploy_units():
            return []
        ended = []
        for task, run in queued_runs():
            if not _dead(run) or run.get("dead_noted"):
                continue
            spec = _read_json(spec_file(task)) or {}
            source, log = _source(task, spec), spec_file(task).with_name("deploy.log")
            if run.get("phase") in CHANGING:
                now = _git(host.live, "rev-parse", "refs/heads/main").strip()
                result = {"status": "stuck", "source": source, "head": spec.get("head"),
                          "summary": f"The deploy unit ended {MAX_ATTEMPTS} times in phase {run['phase']} with the "
                          f"live main at {now[:12]}, moved from {run['old'][:12]} and not verified (see {log}); "
                          "nothing else deploys until it is resumed by hand (specs/WP-09.md §0)."}
                write_json(state_file(task), {**run, "dead_noted": True})
            else:
                summary = (f"The deploy unit ended {MAX_ATTEMPTS} times before it started (see {log}), so nothing was "
                           "deployed; main is unchanged.")
                if run.get("phase") == "landed":
                    summary += " GitHub's main has the change; it goes live with GitHub's main."
                result = {"status": "failed", "source": source, "head": spec.get("head"), "ci": spec.get("ci"),
                          "summary": summary}
                write_json(spec_file(task).with_name("result.json"), result)
                state_file(task).unlink()
            logger.error("The deploy run of %s is dead: %s", task, result["summary"])
            ended.append((task, spec, result))
        return ended
    finally:
        os.close(fd)


def abort_run(task_id: str) -> str:
    """Abort a deploy run by hand, as root:
    ``/root/.openclaw/rmp/venv/bin/python -c 'from app.coding import deploy; print(deploy.abort_run("<task>"))'``.

    Under the launch lock, taken blocking so it waits out a launch, it stops the run's unit, then reads its phase. A
    run still ``queued`` or ``landed`` has changed nothing: it ends ``failed`` in ``result.json`` and its state is
    removed; for GitHub's main, ``NOT_RETRIED`` then keeps the watcher from queueing that head again. A run in a later
    phase keeps its state, and the next idle pass resumes it. What it did, in a sentence."""
    if not RUNS_DIR.is_dir():
        return f"There is no deploy run of {task_id}; nothing was done."
    fd = os.open(RUNS_DIR / ".launch.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # A unit that is not loaded makes systemctl fail; there is nothing to stop then.
        subprocess.run(["systemctl", "stop", f"aura-deploy-{task_id}"], capture_output=True, timeout=300)
        run = _read_json(state_file(task_id))
        if run is None:
            return f"The deploy of {task_id} has no run with work to do; nothing else was done."
        phase = run.get("phase")
        if phase in CHANGING:
            return (f"The deploy of {task_id} is in phase {phase}, past the change, so its state is kept; the next idle "
                    "pass resumes it.")
        spec = _read_json(spec_file(task_id)) or {}
        summary = "Aborted by hand; nothing was deployed; main is unchanged."
        if phase == "landed":
            summary += " GitHub's main has the change; it goes live with GitHub's main."
        write_json(spec_file(task_id).with_name("result.json"),
                   {"status": "failed", "source": _source(task_id, spec), "head": spec.get("head"), "summary": summary})
        state_file(task_id).unlink()
        return f"Aborted the deploy of {task_id} in phase {phase}: it is recorded failed, and its state is removed."
    finally:
        os.close(fd)


def _source(task_id: str, spec: Dict[str, Any]) -> str:
    """Where the run's change comes from, also for a spec that cannot be read."""
    return spec.get("source") or ("github" if task_id == MAIN else "reviewed")


def hand_off(spec: Dict[str, Any], *, live: Path = LIVE_REPO) -> str:
    """Queue a reviewed job's deploy and launch the next run through the gate; the run's unit name.

    Once the run is queued, a launch that fails is logged, not raised: the run stays queued and the reconciler's next
    pass launches it. Raising would fail the activity while the queued run stays on disk.
    """
    unit = queue(spec)
    try:
        next_deploy(Host(live))
    except Exception as exc:
        logger.warning("Launching a deploy after queueing %s failed; the reconciler's next pass launches it: %s",
                       spec["task_id"], exc)
    return unit


MAIN = "main"
# A head whose deploy ended this way waits for a later commit instead of being tried again.
NOT_RETRIED = ("blocked", "failed", "rolled_back")


def watch_main(host: Optional["Host"] = None) -> Optional[str]:
    """The reconciler's pass, every minute: queue GitHub's main once it moved past the live main and CI's test check
    passed on that commit, then launch the next queued run through the gate. The unit started, or None.

    The launcher runs even when GitHub's main cannot be read, so queued runs launch and postpone whatever GitHub
    does; that error is raised after it, for the reconciler's log."""
    host = host or Host()
    try:
        _queue_main(host)
    finally:
        started = next_deploy(host)
    return started


def _queue_main(host: "Host") -> None:
    """Queue GitHub's main unless its deploy unit runs or its run is past ``queued``, which is never replaced."""
    if _unit_active(f"aura-deploy-{MAIN}"):
        return
    run = _read_json(state_file(MAIN))
    if run is not None and run.get("phase") != "queued":
        return
    head = host.fetch_main()
    old = _git(host.live, "rev-parse", "refs/heads/main").strip()
    if head == old or not _is_ancestor(host.live, old, head):
        return
    last = _read_json(spec_file(MAIN).with_name("result.json"))
    if last and last.get("head") == head and last.get("status") in NOT_RETRIED:
        return
    if github.check(head, host.repo) != "success":
        return
    queue({"task_id": MAIN, "source": "github", "head": head, "ci": "success"})


def pending_main(repo: str, *, live: Path = LIVE_REPO) -> Optional[Dict[str, Any]]:
    """GitHub's main when it is not live yet: its commit and how many hours ago it was committed."""
    head = github.main_sha(repo)
    if head == _git(live, "rev-parse", "refs/heads/main").strip():
        return None
    committed = github.api("GET", f"repos/{repo}/commits/{head}")["commit"]["committer"]["date"]
    age = datetime.now(timezone.utc) - datetime.fromisoformat(committed.replace("Z", "+00:00"))
    return {"head": head, "age_hours": round(age.total_seconds() / 3600, 1)}


def _unit_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", unit], capture_output=True).returncode == 0


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _is_ancestor_on_github(commit: str, repo: str) -> bool:
    """Whether GitHub's main contains ``commit``; a commit GitHub has never seen is not."""
    try:
        compare = github.api("GET", f"repos/{repo}/compare/{commit}...main")
    except github.GitHubError:
        return False
    return compare.get("status") in ("identical", "ahead")


def deploy_note(result: Dict[str, Any], repo: str) -> str:
    """Kirill's note after a deploy of Aura's own pull requests."""
    def linked(subject: str) -> str:
        found = re.search(r"\(#(\d+)\)\s*$", subject)
        return f"{subject} https://github.com/{repo}/pull/{found.group(1)}" if found else subject

    headline = {"deployed": "Aura's change is live.", "unchanged": "Aura's change was already live.",
                "rolled_back": "Aura's change failed its checks and was rolled back."}.get(
        result["status"], f"Aura's deploy: {result['status']}.")
    commits = "\n".join(f"• {linked(s)}" for s in result.get("commits") or [])
    return "\n".join(part for part in (headline, commits, result.get("summary", "")) if part)


def open_pull_request(job: workspace.Job, head: str, entry: Dict[str, Any], title: str, body: str,
                      *, gh: str = "gh") -> Dict[str, Any]:
    """Push the approved commit as the job's branch and open a PR; the token is read only by these commands."""
    with workspace.github_auth() as env:
        workspace.git("--git-dir", job.review, "push", "--quiet", workspace.remote_url(entry["remote"]),
                      f"{head}:refs/heads/{job.branch}", env=env)
    token = workspace.TOKEN_FILE.read_text().strip()
    run = subprocess.run([gh, "pr", "create", "--repo", entry["remote"], "--head", job.branch, "--base", entry["branch"],
                          "--title", title, "--body", body], capture_output=True, text=True, timeout=120,
                         env={**os.environ, "GH_TOKEN": token})
    url = next((line.strip() for line in reversed(run.stdout.splitlines()) if line.strip().startswith("https://")), "")
    if run.returncode != 0 or not url:
        detail = (run.stderr or run.stdout).strip()[-300:]
        return {"status": "failed", "summary": f"The branch {job.branch} was pushed, but opening the pull request failed: {detail}"}
    return {"status": "pr_opened", "url": url, "branch": job.branch, "head": head,
            "summary": f"Opened {url} from branch {job.branch} ({head[:12]})."}


class Host:
    """What a self-deploy does to this machine; tests replace it.

    ``checks`` returns ``{"failure": None or what failed, "canary": "ok" or "skipped"}``.
    """

    def __init__(self, live: Path = LIVE_REPO, repo: Optional[str] = None):
        from app.config import get_coding_config

        self.live = live
        self.repo = repo or get_coding_config()["repositories"]["rmp"]["remote"]
        self.born = time.monotonic()

    def elapsed(self) -> float:
        """Seconds since this host was made, at the deploy unit's start."""
        return time.monotonic() - self.born

    def active_user_tasks(self) -> int:
        from app.production.canary_sentinel import count_active_user_tasks_sync

        return count_active_user_tasks_sync(strict=True)

    def deploy_units(self) -> List[str]:
        """The deploy units active or starting now."""
        out = subprocess.run(["systemctl", "list-units", "--plain", "--no-legend", "--state=active,activating",
                              "aura-deploy-*"], check=True, capture_output=True, text=True, timeout=60).stdout
        return [line.split()[0] for line in out.splitlines() if line.strip()]

    def reload_lock_held(self) -> bool:
        """Whether another process holds the code-reload lock: a non-blocking probe, released at once."""
        fd = os.open(CODE_RELOAD_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    @contextmanager
    def hold(self, lock: Path, wait_sec: float) -> Iterator[bool]:
        """Hold the lock file ``lock`` for the ``with`` block, trying every ``LOCK_POLL_SEC`` for up to ``wait_sec``;
        whether it was taken."""
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + wait_sec
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        yield False
                        return
                    time.sleep(min(LOCK_POLL_SEC, left))
            yield True
        finally:
            os.close(fd)

    def systemctl(self, *args: str) -> None:
        subprocess.run(["systemctl", *args], check=True, capture_output=True, timeout=300)

    def pip_install(self) -> None:
        subprocess.run([str(self.live / "venv" / "bin" / "pip"), "install", "-q", "-r", "requirements.txt"],
                       cwd=self.live, check=True, capture_output=True, timeout=1800)

    def fetch_main(self) -> str:
        """GitHub's main, fetched into the live repo; its commit."""
        with workspace.github_auth() as env:
            _git(self.live, "fetch", "--quiet", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main", env=env)
        return _git(self.live, "rev-parse", "refs/remotes/origin/main").strip()

    def land(self, commit: str, branch: str, title: str, body: str, method: str) -> Dict[str, Any]:
        return github.land(self.live, commit, branch, title, body, repo=self.repo, method=method)

    def wait_check(self, sha: str) -> str:
        """CI's test check on ``sha`` once it finished, or its state when the wait ran out."""
        return github.wait_check(sha, self.repo)

    def readiness_baseline(self) -> List[str]:
        from app.coding import deploy_checks

        return deploy_checks.readiness_failures()

    def checks(self, restarted: List[str], baseline: List[str]) -> Dict[str, Any]:
        from app.coding import deploy_checks

        return deploy_checks.run(self.live, restarted, baseline)


def _idle(host: Host, log: Callable[[str], None]) -> bool:
    """Whether no user task is active by the strict count; a count above 0, or one that fails, is logged as waiting."""
    try:
        active = host.active_user_tasks()
    except Exception as exc:
        log(f"waiting: the idle count failed ({str(exc)[:300]})")
        return False
    if active > 0:
        log(f"waiting: {active} active user task(s)")
        return False
    return True


def _affected(live: Path, head: str, changed: List[Tuple[str, str]]) -> List[str]:
    """The units a switch to ``head`` stops and starts: those of the changed paths and of the mirror files the
    reconcile will write, found before anything is written."""
    stale = [path for prefix, target in MIRRORS for _, _, path, _ in _out_of_date(live, head, prefix, target)]
    return restarts_for([path for _, path in changed] + stale)


def _list_stopped(task: str, units: List[str]) -> None:
    """Write the units a window is about to stop, atomically, for the unit's ``ExecStopPost=``."""
    path = stopped_file(task)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text("".join(f"{unit}\n" for unit in units))
    os.replace(tmp, path)


def _start_again(host: Host, task: str, units: List[str], log: Callable[[str], None]) -> None:
    """Start units a window stopped, outside its normal course; once they started the list of stopped units goes. A
    failure here is logged, so the failure that brought the run here is the one handled, and the list stays for the
    unit's ``ExecStopPost=``."""
    if not units:
        return
    try:
        host.systemctl("start", *units)
    except Exception as exc:
        log(f"starting {', '.join(units)} again failed: {exc}")
        return
    log(f"started {', '.join(units)} again")
    stopped_file(task).unlink(missing_ok=True)


def _save(task: str, run: Dict[str, Any], **fields: Any) -> None:
    """Update the run with ``fields`` and write its state, atomically. A deploy called without a queued run (a test's
    direct call) keeps its state in memory only."""
    run.update(fields)
    path = state_file(task)
    if path.exists():
        write_json(path, run)


def _before_applying(run: Dict[str, Any]) -> str:
    """The phase a run had before ``applying``: a reviewed run had landed its pull request."""
    return "landed" if run.get("merge") else "queued"


def _switch(host: Host, task: str, run: Dict[str, Any], direction: str, log: Callable[[str], None]) -> Optional[str]:
    """The switch window: the tree, the mirrors and the requirements change only while the affected units are stopped.

    Forward, ``rmp-api`` stops first, so its graceful stop finishes the requests in flight against the running worker;
    while the tree is still at ``old``, the strict count is then taken again before the other units stop, and a task
    created while the API drained starts it again, puts the run back in its phase before ``applying`` and returns None
    (waiting). Then the tree moves (``merged``): forward to ``head``, back to the recorded revert commit. The mirrors
    are reconciled, the requirements installed and the units started in one call (``restarted``, forward). Back, both
    counts are skipped. Every step finds itself done when a resumed run repeats it. Whatever raises inside, the units
    are started before the window is left; a failure while the live main, read again then, is still at ``old`` also
    puts the run back in its phase before ``applying``. While units are stopped, ``stopped_file`` lists them, so the unit's ``ExecStopPost=`` starts them
    after a kill that runs no Python. The commit the tree is at.
    """
    live, units, forward = host.live, run["affected"], direction == "forward"
    target = run["head"] if forward else run["revert"]
    try:
        before = _live_main(live)
        if units:
            _list_stopped(task, units)
        if "rmp-api" in units:
            host.systemctl("stop", "rmp-api")
            log("stopped rmp-api")
            if forward and before == run["old"] and not _idle(host, log):
                host.systemctl("start", "rmp-api")
                log("started rmp-api again")
                stopped_file(task).unlink(missing_ok=True)
                _save(task, run, phase=_before_applying(run))
                return None
        others = [unit for unit in units if unit != "rmp-api"]
        if others:
            host.systemctl("stop", *others)
            log(f"stopped {', '.join(others)}")
        if before != target:
            _git(live, "merge", "--ff-only", "--quiet", target)
            log(f"main moved {before[:12]}..{target[:12]}")
        if forward:
            _save(task, run, phase="merged")
        changed = changes(live, run["old"] if forward else run["head"], target)
        for prefix, mirror in MIRRORS:
            reconcile(live, target, changed, prefix, mirror)
        if "requirements.txt" in [path for _, path in changed]:
            host.pip_install()
        if units:
            host.systemctl("start", *units)
            log(f"started {', '.join(units)}")
            stopped_file(task).unlink(missing_ok=True)
        if forward:
            _save(task, run, phase="restarted")
    except BaseException:
        _start_again(host, task, units, log)
        if forward and _still_at_old(live, run):
            _save(task, run, phase=_before_applying(run))
        raise
    return target


def _live_main(live: Path) -> str:
    return _git(live, "rev-parse", "refs/heads/main").strip()


def _still_at_old(live: Path, run: Dict[str, Any]) -> bool:
    """Whether the live main is still at the run's ``old``, read again after a failure, whatever the failure
    interrupted; False when it cannot be read: the run then keeps its phase, and its resume reads the live main."""
    try:
        return _live_main(live) == run["old"]
    except Exception:
        return False


def self_deploy(spec: Dict[str, Any], host: Host, *, log: Callable[[str], None] = print) -> Optional[Dict[str, Any]]:
    """The deploy unit's work. Returns the result to record, or None while the run waits: the code-reload lock stayed
    held, less than ``APPLY_RESERVE_SEC`` of the unit's time is left, or a user task was active (or the idle count
    failed) at the final check; the run then keeps its state, and the launcher's next pass tries again.

    ``source`` "github" deploys the spec's ``head``, a commit of GitHub's main that CI passed; otherwise the spec's
    approved ``head`` lands through a pull request first, the run's state records the merge commit (``landed``), and
    exactly that commit is deployed once CI's test check passed on it. That slow work runs first, without the lock.
    Then, holding the code-reload lock: the hand-edit check, the readiness baseline and, last before the units stop,
    the strict idle count; then ``applying`` and the switch window (``_switch``), the checks, and on failure the revert
    window. A run whose state is past ``landed`` was killed during or after its change: it resumes from its phase
    (``_resume``) and runs no step before the change.
    """
    task, live = spec["task_id"], host.live
    run = _read_json(state_file(task))
    if run is not None and run.get("phase") in CHANGING:
        with host.hold(CODE_RELOAD_LOCK, LOCK_WAIT_SEC) as held:
            if not held:
                log(f"waiting: {CODE_RELOAD_LOCK} stayed held for {LOCK_WAIT_SEC} s; the run keeps its phase")
                return None
            log(f"holding {CODE_RELOAD_LOCK}")
            return _resume(host, task, run, log)
    run = run or {}
    old = _git(live, "rev-parse", "refs/heads/main").strip()
    if spec.get("source") == "github":
        host.fetch_main()
        head = spec["head"]
        if _is_ancestor(live, head, old):
            return {"status": "unchanged", "old": old, "head": head, "restarted": [], "commits": [],
                    "summary": f"GitHub's main ({head[:12]}) is already live, so nothing changed."}
        if not _is_ancestor(live, old, head):
            return {"status": "blocked", "summary": f"The live main ({old[:12]}) has commits GitHub's main lacks, so nothing "
                    "was deployed; main is unchanged."}
    else:
        if old != spec["old"]:
            return {"status": "failed", "summary": "main moved while the deploy waited, so nothing was deployed. Ask me again."}
        blocked = _drift(live, old)
        if blocked:
            return blocked
        if run.get("phase") != "landed":
            landed = host.land(spec["head"], spec["branch"], spec["title"], spec["body"], "squash")
            if landed["status"] != "merged":
                return {"status": "blocked", "pr": landed["pr"], "summary": f"CI's test check on {landed['pr']['url']} "
                        f"is {landed['check']}, so nothing was deployed; main is unchanged."}
            _save(task, run, phase="landed", pr=landed["pr"], merge=landed["merge"])
        head = run["merge"]
        log(f"landed {run['pr']['url']} as {head[:12]}")
        host.fetch_main()
        check = host.wait_check(head)
        if check != "success":
            return {"status": "blocked", "pr": run["pr"], "summary": f"CI's test check on the merged commit "
                    f"{head[:12]} is {check}, so nothing was deployed here; main is unchanged. GitHub's main has the "
                    "change; it goes live with GitHub's main once CI passes on a later commit."}
    with host.hold(CODE_RELOAD_LOCK, LOCK_WAIT_SEC) as held:
        if not held:
            log(f"waiting: {CODE_RELOAD_LOCK} stayed held for {LOCK_WAIT_SEC} s; the run keeps its phase")
            return None
        log(f"holding {CODE_RELOAD_LOCK}")
        changed = changes(live, old, head)
        baseline = host.readiness_baseline()
        # After the baseline, which can take a minute and a half: a hand edit made meanwhile still blocks.
        blocked = _drift(live, old)
        if blocked:
            return blocked
        run.update(old=old, head=head, affected=_affected(live, head, changed), baseline=baseline)
        return _apply_when_idle(host, task, run, log)


def _apply_when_idle(host: Host, task: str, run: Dict[str, Any],
                     log: Callable[[str], None]) -> Optional[Dict[str, Any]]:
    """From the time reserve on: the reserve, the final strict count, ``applying``, then the change. A resumed run
    still at ``old`` that waits here goes back to its phase before ``applying``, like a run that never reached it."""
    reserved = host.elapsed() <= DEPLOY_UNIT_MAX_SEC - APPLY_RESERVE_SEC
    if not reserved:
        log(f"waiting: less than {APPLY_RESERVE_SEC // 60} min of the unit's {DEPLOY_UNIT_MAX_SEC // 3600} h are left "
            "for the change")
    if not reserved or not _idle(host, log):
        if run.get("phase") == "applying":
            _save(task, run, phase=_before_applying(run))
        return None
    _save(task, run, phase="applying")
    return _change(host, task, run, log)


def _resume(host: Host, task: str, run: Dict[str, Any], log: Callable[[str], None]) -> Optional[Dict[str, Any]]:
    """Continue a run killed during or after its change, from its phase and the live main, with the ``old``,
    ``head``, units and baseline its state recorded; no step before the change runs again."""
    phase, now = run["phase"], _git(host.live, "rev-parse", "refs/heads/main").strip()
    log(f"resuming from phase {phase}, the live main at {now[:12]}")
    if phase == "applying" and now == run["old"]:
        if run["affected"]:
            host.systemctl("start", *run["affected"])
            log(f"started {', '.join(run['affected'])}")
            stopped_file(task).unlink(missing_ok=True)
        return _apply_when_idle(host, task, run, log)
    if phase in ("applying", "merged", "restarted") and now == run["head"]:
        return _change(host, task, run, log)
    if phase == "reverting" and now in (run["head"], run.get("revert")):
        return _rollback(host, task, run, log)
    _start_again(host, task, run["affected"], log)
    return {"status": "failed",
            "summary": f"The live main is {now[:12]}, which this deploy did not make; please check it."}


def _change(host: Host, task: str, run: Dict[str, Any], log: Callable[[str], None]) -> Optional[Dict[str, Any]]:
    """The switch window (unless the run restarted its units already), then the checks: the result, or None while the
    API's drain found a task. A failure once the tree moved is a rollback; one before it is raised."""
    try:
        if run["phase"] != "restarted" and _switch(host, task, run, "forward", log) is None:
            return None
        checked = host.checks(run["affected"], run["baseline"])
    except Exception as exc:
        # A rollback only once the tree moved, by the live main; a main that cannot be read raises, and the run's
        # resume reads it.
        if run["phase"] not in CHANGING or _live_main(host.live) == run["old"]:
            raise
        checked = {"failure": f"applying the change failed ({str(exc)[:300]})", "canary": None}
    failure = checked["failure"]
    if failure is not None:
        log(f"verification failed: {failure}; reverting")
        _save(task, run, phase="reverting", failure=failure)
        return _rollback(host, task, run, log)
    old, head, restarted = run["old"], run["head"], run["affected"]
    canary = "the canary passed" if checked["canary"] == "ok" else "the canary was skipped because Aura was busy"
    units = sorted(path[len(UNIT_DIR):] for _, path in changes(host.live, old, head) if path.startswith(UNIT_DIR))
    installs = (f" Files under {UNIT_DIR} changed ({', '.join(units)}): the deploy does not install them; an "
                "operator reviews them, runs ops/install_units.py, then --apply --head with the sha it printed, in "
                "an idle window." if units else "")
    return {"status": "deployed", **_shipped(host, run), "canary": checked["canary"], "units": units,
            "summary": f"Deployed {head[:12]} to main. Restarted {', '.join(restarted) or 'nothing'}; health and "
            f"readiness passed and {canary}.{installs}"}


def _shipped(host: Host, run: Dict[str, Any]) -> Dict[str, Any]:
    return {"old": run["old"], "head": run["head"], "restarted": run["affected"],
            "commits": _subjects(host.live, run["old"], run["head"]), **({"pr": run["pr"]} if run.get("pr") else {})}


def _rollback(host: Host, task: str, run: Dict[str, Any], log: Callable[[str], None]) -> Dict[str, Any]:
    """The revert window, the re-check and the revert's pull request. The revert commit (the tree of ``old`` on top
    of ``head``) is made and recorded before the tree moves to it, so a resumed rollback finds it and moves the tree
    there, or finds the tree moved already."""
    live, head, failure, restarted = host.live, run["head"], run["failure"], run["affected"]
    if not run.get("revert"):
        revert = _git(live, "-c", "user.name=RMP deploy", "-c", "user.email=rmp@aura.local", "commit-tree",
                      f"{run['old']}^{{tree}}", "-p", head, "-m",
                      f"Revert {head[:12]}: the deploy's checks failed ({failure[:200]})").strip()
        _save(task, run, revert=revert)
    revert = _switch(host, task, run, "back", log)
    still = host.checks(restarted, run["baseline"])["failure"]
    published = _publish_revert(host, revert, head, failure, log)
    health = "Aura is healthy again" if still is None else f"the checks still fail: {still}"
    if published["status"] == "merged":
        github_now = f"GitHub's main is reverted too ({published['pr']['url']})"
    elif published.get("pr"):
        github_now = f"on GitHub the revert waits in {published['pr']['url']} (its test check is {published['check']})"
    else:
        github_now = f"the revert is not on GitHub yet ({published['error']})"
    return {"status": "rolled_back", **_shipped(host, run), "revert": revert, "revert_pr": published, "error": failure,
            "summary": f"Deploying {head[:12]} failed its checks ({failure}), so I reverted main ({revert[:12]}) and restarted "
            f"{', '.join(restarted) or 'nothing'}; {health}; {github_now}."}


def _drift(live: Path, old: str) -> Optional[Dict[str, Any]]:
    """A blocked result when any mirror holds a hand edit, whether or not the change touches that mirror."""
    for prefix, target in MIRRORS:
        drift = drifted(live, old, prefix, target)
        if drift:
            return {"status": "blocked", "summary": f"The live copy of {prefix} was edited by hand ({', '.join(drift[:5])}), "
                    "so nothing was deployed; main is unchanged."}
    return None


def _is_ancestor(live: Path, old: str, head: str) -> bool:
    try:
        _git(live, "merge-base", "--is-ancestor", old, head)
    except RuntimeError:
        return False
    return True


def _subjects(live: Path, old: str, head: str) -> List[str]:
    return _git(live, "log", "--format=%s", f"{old}..{head}").splitlines()[:20]


def _publish_revert(host: Host, revert: str, head: str, failure: str, log: Callable[[str], None]) -> Dict[str, Any]:
    """The live revert onto GitHub's main through a pull request of its own. A merge commit, not a squash, keeps the
    live main an ancestor of GitHub's, so the next deploy fast-forwards."""
    try:
        return host.land(revert, f"aura/revert-{head[:12]}", f"Revert {head[:12]}: the deploy's checks failed",
                         f"RMP deployed {head[:12]}, its checks failed ({failure[:300]}), and RMP reverted the live code at "
                         "once. This brings GitHub's main to the same state.", "merge")
    except Exception as exc:
        log(f"publishing the revert failed: {exc}")
        return {"status": "failed", "error": str(exc)[:300]}
