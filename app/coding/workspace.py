"""Job checkouts for coding tasks: cloned by root, worked in as aura-coder, reviewed by root.

Root clones a trusted source (the live RMP repo, or a root-only mirror of a GitHub repo fetched with
the token) into ``/srv/aura-code/jobs/<task>`` and hands it to aura-coder. After that root runs no git
in the checkout: a planted ``.git/config`` (fsmonitor, textconv, filter drivers) or hook would run as
root, or misreport the diff. Leftover changes are committed and the work exported as a bundle by
aura-coder in a hardened unit; root fetches the bundle into its own review repository and reads the
commits, diff and changed paths there. Deploys take their commit from the review repository too.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.coding.units import CODER_HOME, CODER_USER, CODE_ROOT, JOBS_DIR, RUNS_DIR, systemd_run_argv, unit_properties

REPOS_DIR = CODE_ROOT / "repos"
REVIEW_DIR = CODE_ROOT / "review"
TOKEN_FILE = Path("/root/.config/github_pat")
GIT_NAME, GIT_EMAIL = "Aura (Claude Code)", "aura-coder@aura.local"
# Kept out of commits: RMP's own files in the checkout, and environments the tests build.
EXCLUDES = (".aura/", ".venv/", "node_modules/", "__pycache__/", ".pytest_cache/")
MAX_BUNDLE_BYTES = 200 * 1024 * 1024
TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|[^/]*\.test\.[cm]?[jt]s$)")
DEPENDENCY_FILES = {"requirements.txt", "pyproject.toml", "package.json", "package-lock.json", "setup.py", "setup.cfg"}

COLLECT_SCRIPT = r"""
set -e
export GIT_NO_REPLACE_OBJECTS=1
g() { git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c user.name="$AURA_GIT_NAME" -c user.email="$AURA_GIT_EMAIL" "$@"; }
g add -A
if ! g diff --cached --quiet; then g commit -q -m "$AURA_COMMIT_MESSAGE"; fi
rm -f .aura/job.bundle
if [ "$(g rev-list --count "$AURA_BASE..HEAD")" != "0" ]; then g bundle create -q .aura/job.bundle HEAD "^$AURA_BASE"; fi
"""


@dataclass
class Job:
    task_id: str
    repo: str
    branch: str
    base: str
    source: str
    checkout: str
    review: str
    created_at: str


def git(*args: str, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None, timeout: int = 900) -> str:
    """git as root, for trusted repositories only (sources, mirrors, review repositories)."""
    run = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, capture_output=True, text=True,
                         timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
    if run.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {run.stderr.strip()[-400:]}")
    return run.stdout


@contextmanager
def github_auth() -> Iterator[Dict[str, str]]:
    """GIT_ASKPASS that reads the token file at use; the token never lands in a URL or config."""
    fd, path = tempfile.mkstemp(prefix="aura-askpass-")
    with os.fdopen(fd, "w") as fh:
        fh.write(f"#!/bin/sh\ncase \"$1\" in *Username*) echo x-access-token ;; *) tr -d '\\n\\r' < {TOKEN_FILE} ;; esac\n")
    os.chmod(path, 0o700)
    try:
        yield {"GIT_ASKPASS": path}
    finally:
        os.unlink(path)


def remote_url(remote: str) -> str:
    return f"https://github.com/{remote}.git"


def refresh_mirror(remote: str) -> Path:
    mirror = REPOS_DIR / f"{remote.split('/')[-1]}.git"
    REPOS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with github_auth() as env:
        if mirror.exists():
            git("--git-dir", str(mirror), "fetch", "--quiet", "--prune", "origin", env=env)
        else:
            git("clone", "--quiet", "--mirror", remote_url(remote), str(mirror), env=env)
    return mirror


def branch_name(task_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "change"
    return f"aura/{task_id[:8]}-{slug}"


def job_file(task_id: str) -> Path:
    return RUNS_DIR / task_id / "job.json"


def load_job(task_id: str) -> Optional[Job]:
    path = job_file(task_id)
    return Job(**json.loads(path.read_text())) if path.exists() else None


def _chown_tree(root: Path, owner: str) -> None:
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            shutil.chown(path, owner, owner)


def prepare(task_id: str, repo: str, title: str, cfg: Dict[str, Any], *, owner: Optional[str] = CODER_USER) -> Job:
    """The job's checkout on a new branch, and root's review repository at the same base."""
    existing = load_job(task_id)
    if existing:
        return existing
    entry = cfg["repositories"][repo]
    source = entry.get("source") or str(refresh_mirror(entry["remote"]))
    checkout, review = JOBS_DIR / task_id, REVIEW_DIR / f"{task_id}.git"
    for path in (checkout, review):
        shutil.rmtree(path, ignore_errors=True)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    # --no-hardlinks: a hardlinked object would change owner in the source too.
    clone = ["clone", "--quiet", "--no-hardlinks", "--single-branch", "--branch", entry["branch"]]
    git(*clone, source, str(checkout))
    git(*clone, "--bare", source, str(review))
    base = git("rev-parse", "HEAD", cwd=checkout).strip()
    branch = branch_name(task_id, title)
    git("checkout", "--quiet", "-b", branch, cwd=checkout)
    git("remote", "set-url", "origin", remote_url(entry["remote"]), cwd=checkout)
    git("config", "user.name", GIT_NAME, cwd=checkout)
    git("config", "user.email", GIT_EMAIL, cwd=checkout)
    with (checkout / ".git" / "info" / "exclude").open("a") as fh:
        fh.write("\n" + "\n".join(EXCLUDES) + "\n")
    (checkout / ".aura").mkdir()
    if owner:
        _chown_tree(checkout, owner)
    os.chmod(checkout, 0o700)
    job = Job(task_id=task_id, repo=repo, branch=branch, base=base, source=source, checkout=str(checkout),
              review=str(review), created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    job_file(task_id).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_file(task_id).write_text(json.dumps(asdict(job), indent=2) + "\n")
    return job


def _export(job: Job, message: str, number: int) -> None:
    """Commit leftovers and bundle the work, as aura-coder in a hardened unit."""
    checkout = Path(job.checkout)
    props = unit_properties(writable=[checkout, CODER_HOME], memory_max="1G", cpu_quota="100%", tasks_max=256,
                            runtime_max_sec=600)
    env = {"AURA_GIT_NAME": GIT_NAME, "AURA_GIT_EMAIL": GIT_EMAIL, "AURA_BASE": job.base,
           "AURA_COMMIT_MESSAGE": message.strip()[:2000] or "Aura (Claude Code) changes"}
    argv = systemd_run_argv(f"aura-collect-{job.task_id}-{number}", ["/bin/sh", "-c", COLLECT_SCRIPT],
                            properties=props, workdir=checkout, env=env, wait=True)
    run = subprocess.run(argv, capture_output=True, text=True, timeout=660, stdin=subprocess.DEVNULL)
    if run.returncode != 0:
        raise RuntimeError(f"collecting the work failed (exit {run.returncode}): {(run.stderr or run.stdout)[-400:]}")


def collect(job: Job, message: str, cfg: Dict[str, Any], *, number: int = 1) -> Dict[str, Any]:
    """Commits, diffstat, bounded diff, changed paths and secret findings, read in the review repository."""
    _export(job, message, number)
    bundle = Path(job.checkout) / ".aura" / "job.bundle"
    empty = {"base": job.base, "head": job.base, "branch": job.branch, "commits": [], "diffstat": "", "changed": [],
             "diff": "", "truncated": False, "secrets": [], "tests_changed": [], "dependencies_changed": []}
    if not bundle.exists():
        return empty
    if bundle.stat().st_size > MAX_BUNDLE_BYTES:
        raise RuntimeError(f"the work's bundle is over {MAX_BUNDLE_BYTES >> 20} MiB")
    kept = RUNS_DIR / job.task_id / f"collect-{number}.bundle"
    shutil.copyfile(bundle, kept)
    review = ["--git-dir", job.review]
    git(*review, "bundle", "verify", "-q", str(kept))
    git(*review, "fetch", "--quiet", "--no-tags", str(kept), "+HEAD:refs/aura/head")
    head = git(*review, "rev-parse", "refs/aura/head").strip()
    try:
        git(*review, "merge-base", "--is-ancestor", job.base, head)
    except RuntimeError:
        raise RuntimeError(f"the work no longer builds on {job.base[:12]}")
    log = git(*review, "log", "--no-merges", "--format=%H%x1f%an%x1f%ae%x1f%s", f"{job.base}..{head}")
    commits = [dict(zip(("sha", "author", "email", "subject"), line.split("\x1f"))) for line in log.splitlines()]
    changed = [line.split("\t", 1) for line in git(*review, "diff", "--name-status", "--no-renames", job.base, head).splitlines()]
    diff = git(*review, "diff", "--no-textconv", "--no-ext-diff", job.base, head)
    limit = int(cfg.get("diff_limit_chars", 200_000))
    paths = [path for _, path in changed]
    return {**empty, "head": head, "commits": commits, "changed": [{"status": s, "path": p} for s, p in changed],
            "diffstat": git(*review, "diff", "--stat", job.base, head), "diff": diff[:limit], "truncated": len(diff) > limit,
            "secrets": secret_scan(diff), "tests_changed": [p for p in paths if TEST_PATH.search(p)],
            "dependencies_changed": [p for p in paths if Path(p).name in DEPENDENCY_FILES]}


SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}|\bxapp-\d-[A-Za-z0-9-]{10,}")),
    ("Anthropic key", re.compile(r"\bsk-ant-[a-z0-9]+-[A-Za-z0-9_-]{20,}")),
    ("OpenAI key", re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}")),
    ("NVIDIA key", re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
]
SECRET_FILES = [Path("/etc/rmp/rmp.env"), Path("/etc/openclaw/openclaw.env"), Path("/etc/aura-coder/claude.env"),
                Path("/root/.config/github_pat")]
SECRET_CONFIGS = [Path("/root/.openclaw/openclaw.json"), Path("/root/.openclaw/rmp/settings.json")]
_SECRET_KEY = re.compile(r"(token|key|secret|password|pat)$", re.I)


def known_secret_values() -> List[str]:
    """This host's secret values (root only): env files, the GitHub token, secrets inside configs."""
    values: List[str] = []
    for path in SECRET_FILES:
        try:
            for line in path.read_text().splitlines():
                value = line.split("=", 1)[1] if "=" in line else line
                values.append(value.strip().strip("'\""))
        except OSError:
            continue

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, str) and _SECRET_KEY.search(key):
            values.append(node)

    for path in SECRET_CONFIGS:
        try:
            walk(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return sorted({v for v in values if len(v) >= 12 and not v.startswith("${")})


def secret_scan(diff: str, known: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Secrets on added lines of a unified diff: known patterns and this host's own secret values."""
    known = known_secret_values() if known is None else known
    findings: List[Dict[str, Any]] = []
    path = ""
    for number, line in enumerate(diff.splitlines(), 1):
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
            continue
        if not line.startswith("+"):
            continue
        kinds = [name for name, rx in SECRET_PATTERNS if rx.search(line)]
        kinds += ["this host's secret"] if any(value in line for value in known) else []
        for kind in kinds:
            findings.append({"path": path, "diff_line": number, "kind": kind})
    return findings


def prune(days: int, *, now: Optional[datetime] = None) -> List[str]:
    """Remove checkouts and review repositories of jobs older than ``days``, unless one of their runs is live."""
    from app.coding.runner import Run, unit_active

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    removed = []
    for path in sorted(RUNS_DIR.glob("*/job.json")):
        job = Job(**{k: v for k, v in json.loads(path.read_text()).items() if k in Job.__dataclass_fields__})
        if datetime.fromisoformat(job.created_at) > cutoff or not Path(job.checkout).exists():
            continue
        numbers = [int(p.name) for p in path.parent.iterdir() if p.is_dir() and p.name.isdigit()]
        if any(unit_active(Run(job.task_id, n).unit) for n in numbers):
            continue
        shutil.rmtree(job.checkout, ignore_errors=True)
        shutil.rmtree(job.review, ignore_errors=True)
        removed.append(job.task_id)
    return removed
