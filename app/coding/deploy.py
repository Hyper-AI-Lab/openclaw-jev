"""Shipping Aura's code: GitHub's main is protected, so every change lands there through a pull request first.

Two changes ship this way. Aura's own pull requests: Claude merges one once she approves and CI passed, and
``watch_main`` starts the deploy of GitHub's main once CI passed on that commit too. A reviewed coding
job's approved commit: the worker fetches it into the live repo, checks it fast-forwards ``main`` and runs
the full suite on that exact commit as aura-coder, then hands off; the unit lands it through a pull request.

The detached root unit (``ops/coding_deploy.py``) holds the code-reload lock, waits until no user task is
active, refuses mirrors that drifted, fast-forwards the live ``main`` to GitHub's, syncs the mirrors,
restarts only what changed and checks health, readiness and a canary. On failure it reverts the live
``main`` and restarts at once; the revert reaches GitHub as a pull request of its own. It records the
result, then a reviewed job's workflow replies on the new code, or Kirill gets a note for Aura's own.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.coding import github, verify, workspace
from app.coding.units import CODE_ROOT, CODER_USER, JOBS_DIR, RUNS_DIR

LIVE_REPO = Path("/root/.openclaw/rmp")
BUNDLES_DIR = CODE_ROOT / "bundles"
CODE_RELOAD_LOCK = Path("/run/rmp-code-reload.lock")
IDLE_WAIT_SEC = 2 * 3600
IDLE_POLL_SEC = 15
DEPLOY_UNIT_MAX_SEC = 3 * 3600
# (path or directory prefix, services restarted when it changes); docs, tests and ops restart nothing.
SERVICE_PATHS = (
    ("app/", ("rmp-api", "rmp-worker")),
    ("worker.py", ("rmp-api", "rmp-worker")),
    ("requirements.txt", ("rmp-api", "rmp-worker")),
    ("plugins/", ("openclaw-gateway",)),
    ("web-stack/", ("aura-web-backends",)),
)
# Repo directories with a live copy elsewhere on the host.
MIRRORS = (
    ("plugins/", Path("/root/.openclaw/plugins")),
    ("web-stack/", Path("/root/.openclaw/web-stack")),
    ("systemd/", Path("/etc/systemd/system")),
    (".cursor/rules/", Path("/root/.cursor/rules")),
)


def restarts_for(paths: Iterable[str]) -> List[str]:
    """The services a deploy restarts: the API and worker for app code, a changed unit under systemd/ itself."""
    services = set()
    for path in paths:
        if path.startswith("systemd/") and path.endswith((".service", ".timer")):
            name = PurePosixPath(path).name
            services.add(name.removesuffix(".service"))
        for prefix, units in SERVICE_PATHS:
            if path == prefix or (prefix.endswith("/") and path.startswith(prefix)):
                services.update(units)
    return sorted(services)


def _git(repo: Path, *args: str, env: Optional[Dict[str, str]] = None) -> str:
    return workspace.git(*args, cwd=repo, env=env)


def changes(repo: Path, old: str, new: str) -> List[Tuple[str, str]]:
    out = _git(repo, "diff", "--name-status", "--no-renames", old, new)
    return [tuple(line.split("\t", 1)) for line in out.splitlines()]


def drifted(repo: Path, old: str, prefix: str, target: Path) -> List[str]:
    """Tracked files of a mirror whose live copy matches no committed version: someone edited the live copy.

    A live copy that is merely behind (an earlier committed version) is not drift; the sync updates it.
    """
    found = []
    for path in _git(repo, "ls-tree", "-r", "--name-only", old, "--", prefix).splitlines():
        live = target / path[len(prefix):]
        if not live.is_file():
            found.append(f"{path} (missing)")
            continue
        blob = _git(repo, "hash-object", str(live)).strip()
        if blob == _git(repo, "rev-parse", f"{old}:{path}").strip():
            continue
        history = _git(repo, "log", "--format=", "--raw", "--no-abbrev", old, "--", path).split()
        if blob not in history:
            found.append(path)
    return found


def sync(repo: Path, new: str, changed: List[Tuple[str, str]], prefix: str, target: Path) -> List[str]:
    """Write the commit's version of each changed file under ``prefix`` into the mirror; delete removed ones."""
    written = []
    for status, path in changed:
        if not path.startswith(prefix):
            continue
        dest = target / path[len(prefix):]
        if status == "D":
            dest.unlink(missing_ok=True)
        else:
            mode = _git(repo, "ls-tree", new, "--", path).split()[0]
            data = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "show", f"{new}:{path}"], cwd=repo,
                                  capture_output=True, check=True).stdout
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{dest.name}.aura-deploy")
            tmp.write_bytes(data)
            os.chmod(tmp, 0o755 if mode == "100755" else 0o644)
            os.replace(tmp, dest)
        written.append(path)
    return written


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


def hand_off(spec: Dict[str, Any], *, live: Path = LIVE_REPO) -> str:
    """Start the detached deploy unit; it outlives worker restarts, including its own.

    The spec stays on disk only once the unit started, so a retried activity finding it does not deploy twice.
    While a deploy of GitHub's main runs, a later commit waits for the next one.
    """
    path = spec_file(spec["task_id"])
    unit = f"aura-deploy-{spec['task_id']}"
    if spec.get("source") == "github":
        if _unit_active(unit):
            return unit
    elif path.exists() and json.loads(path.read_text()).get("head") == spec["head"]:
        return unit
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(spec, indent=2) + "\n")
    log = path.with_name("deploy.log")
    props = [f"RuntimeMaxSec={DEPLOY_UNIT_MAX_SEC}", f"StandardOutput=file:{log}", "StandardError=inherit",
             f"Environment=PYTHONPATH={live}", "EnvironmentFile=-/etc/rmp/rmp.env", "EnvironmentFile=-/etc/openclaw/openclaw.env"]
    argv = ["systemd-run", f"--unit={unit}", f"--working-directory={live}", "--collect", "--quiet",
            *[f"--property={prop}" for prop in props], "--",
            str(live / "venv" / "bin" / "python"), str(live / "ops" / "coding_deploy.py"), str(path)]
    try:
        subprocess.run(argv, check=True, capture_output=True, timeout=60)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return unit


MAIN = "main"
# A head whose deploy ended this way waits for a later commit instead of being tried again.
NOT_RETRIED = ("blocked", "failed", "rolled_back")


def watch_main(host: Optional["Host"] = None) -> Optional[str]:
    """Start the deploy of GitHub's main once it moved past the live main and CI's test check passed on that commit;
    the unit then waits until Aura is idle. The unit started, or None."""
    host = host or Host()
    if _unit_active(f"aura-deploy-{MAIN}"):
        return None
    head = host.fetch_main()
    old = _git(host.live, "rev-parse", "refs/heads/main").strip()
    if head == old or not _is_ancestor(host.live, old, head):
        return None
    last = _read_json(spec_file(MAIN).with_name("result.json"))
    if last and last.get("head") == head and last.get("status") in NOT_RETRIED:
        return None
    if github.check(head, host.repo) != "success":
        return None
    return hand_off({"task_id": MAIN, "source": "github", "head": head, "ci": "success"}, live=host.live)


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

    def active_user_tasks(self) -> int:
        from app.production.canary_sentinel import count_active_user_tasks_sync

        return count_active_user_tasks_sync(strict=True)

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

    def readiness_baseline(self) -> List[str]:
        from app.coding import deploy_checks

        return deploy_checks.readiness_failures()

    def checks(self, restarted: List[str], baseline: List[str]) -> Dict[str, Any]:
        from app.coding import deploy_checks

        return deploy_checks.run(self.live, restarted, baseline)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _apply(host: Host, old: str, new: str, changed: List[Tuple[str, str]]) -> List[str]:
    """Sync the mirrors, install requirements and restart what changed between two commits; the services restarted."""
    paths = [path for _, path in changed]
    for prefix, target in MIRRORS:
        sync(host.live, new, changed, prefix, target)
    if "requirements.txt" in paths:
        host.pip_install()
    services = restarts_for(paths)
    if any(path.startswith("systemd/") for path in paths):
        host.systemctl("daemon-reload")
    if services:
        host.systemctl("restart", *services)
    return services


def self_deploy(spec: Dict[str, Any], host: Host, *, log: Callable[[str], None] = print) -> Dict[str, Any]:
    """The deploy unit's work, once it holds the code-reload lock. Returns the result to record.

    ``source`` "github" deploys the spec's ``head``, a commit of GitHub's main that CI passed; otherwise the spec's
    approved ``head`` lands through a pull request first.
    """
    live = host.live
    waited = 0
    while host.active_user_tasks() > 0:
        if waited >= IDLE_WAIT_SEC:
            return {"status": "postponed", "summary": "Aura stayed busy for 2 hours, so nothing was deployed; main is unchanged."}
        host.sleep(IDLE_POLL_SEC)
        waited += IDLE_POLL_SEC
    old = _git(live, "rev-parse", "refs/heads/main").strip()
    landed = None
    if spec.get("source") == "github":
        host.fetch_main()
        head = spec["head"]
        if head == old:
            return {"status": "unchanged", "old": old, "head": head, "restarted": [], "commits": [],
                    "summary": f"GitHub's main ({head[:12]}) was already live, so nothing changed."}
        if not _is_ancestor(live, old, head):
            return {"status": "blocked", "summary": f"The live main ({old[:12]}) has commits GitHub's main lacks, so nothing "
                    "was deployed; main is unchanged."}
    else:
        if old != spec["old"]:
            return {"status": "failed", "summary": "main moved while the deploy waited, so nothing was deployed. Ask me again."}
        blocked = _drift(live, old, changes(live, old, spec["head"]))
        if blocked:
            return blocked
        landed = host.land(spec["head"], spec["branch"], spec["title"], spec["body"], "squash")
        if landed["status"] != "merged":
            return {"status": "blocked", "pr": landed["pr"], "summary": f"CI's test check on {landed['pr']['url']} is "
                    f"{landed['check']}, so nothing was deployed; main is unchanged."}
        head = host.fetch_main()
        log(f"landed {landed['pr']['url']} as {head[:12]}")
    changed = changes(live, old, head)
    blocked = _drift(live, old, changed)
    if blocked:
        return blocked
    baseline = host.readiness_baseline()
    _git(live, "merge", "--ff-only", "--quiet", head)
    log(f"main fast-forwarded {old[:12]}..{head[:12]}")
    restarted = restarts_for(path for _, path in changed)
    try:
        restarted = _apply(host, old, head, changed)
        log(f"restarted {restarted or 'nothing'}")
        checked = host.checks(restarted, baseline)
    except Exception as exc:
        checked = {"failure": f"applying the change failed ({str(exc)[:300]})", "canary": None}
    failure = checked["failure"]
    shipped = {"old": old, "head": head, "restarted": restarted, "commits": _subjects(live, old, head),
               **({"pr": landed["pr"]} if landed else {})}
    if failure is None:
        canary = "the canary passed" if checked["canary"] == "ok" else "the canary was skipped because Aura was busy"
        return {"status": "deployed", **shipped, "canary": checked["canary"],
                "summary": f"Deployed {head[:12]} to main. Restarted {', '.join(restarted) or 'nothing'}; health and "
                f"readiness passed and {canary}."}
    log(f"verification failed: {failure}; reverting")
    _git(live, "revert", "--no-edit", "--no-commit", f"{old}..{head}")
    _git(live, "-c", "user.name=RMP deploy", "-c", "user.email=rmp@aura.local", "commit", "--quiet",
         "-m", f"Revert {head[:12]}: the deploy's checks failed ({failure[:200]})")
    revert = _git(live, "rev-parse", "HEAD").strip()
    _apply(host, head, revert, changes(live, head, revert))
    still = host.checks(restarted, baseline)["failure"]
    published = _publish_revert(host, revert, head, failure, log)
    health = "Aura is healthy again" if still is None else f"the checks still fail: {still}"
    if published["status"] == "merged":
        github_now = f"GitHub's main is reverted too ({published['pr']['url']})"
    elif published.get("pr"):
        github_now = f"on GitHub the revert waits in {published['pr']['url']} (its test check is {published['check']})"
    else:
        github_now = f"the revert is not on GitHub yet ({published['error']})"
    return {"status": "rolled_back", **shipped, "revert": revert, "revert_pr": published, "error": failure,
            "summary": f"Deploying {head[:12]} failed its checks ({failure}), so I reverted main ({revert[:12]}) and restarted "
            f"{', '.join(restarted) or 'nothing'}; {health}; {github_now}."}


def _drift(live: Path, old: str, changed: List[Tuple[str, str]]) -> Optional[Dict[str, Any]]:
    for prefix, target in MIRRORS:
        if any(path.startswith(prefix) for _, path in changed):
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
