"""Aura's repository on GitHub, where main is protected: every change lands through a pull request whose test check passed.

The token is read only inside the command that uses it (gh gets it as GH_TOKEN, git through GIT_ASKPASS),
as the github-access rule says. RMP merges Aura's pull requests when she asks (``deploy_pr``), lands a
reviewed job's approved commit the same way, and turns a failed deploy's revert into a pull request.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.coding import workspace

CHECK = "test"
CHECK_POLL_SEC = 20
CHECK_WAIT_SEC = 1800


class GitHubError(Exception):
    pass


def api(method: str, path: str, body: Optional[Dict[str, Any]] = None, *, gh: str = "gh") -> Any:
    args = [gh, "api", "--method", method, path] + (["--input", "-"] if body is not None else [])
    run = subprocess.run(args, input=json.dumps(body) if body is not None else None, capture_output=True, text=True,
                         timeout=120, env={**os.environ, "GH_TOKEN": workspace.TOKEN_FILE.read_text().strip()})
    if run.returncode != 0:
        raise GitHubError(f"{method} {path}: {(run.stderr or run.stdout).strip()[-300:]}")
    return json.loads(run.stdout) if run.stdout.strip() else {}


def pull(number: int, repo: str) -> Dict[str, Any]:
    return api("GET", f"repos/{repo}/pulls/{int(number)}")


def main_sha(repo: str) -> str:
    return api("GET", f"repos/{repo}/git/ref/heads/main")["object"]["sha"]


def check(sha: str, repo: str) -> str:
    """The test check on a commit: "success", "failure", "pending", or "missing" before CI has started."""
    runs = api("GET", f"repos/{repo}/commits/{sha}/check-runs?check_name={CHECK}").get("check_runs") or []
    if not runs:
        return "missing"
    latest = max(runs, key=lambda r: r.get("started_at") or "")
    if latest.get("status") != "completed":
        return "pending"
    return "success" if latest.get("conclusion") == "success" else "failure"


def wait_check(sha: str, repo: str, *, timeout: float = CHECK_WAIT_SEC,
               sleep: Callable[[float], None] = time.sleep) -> str:
    deadline = time.monotonic() + timeout
    while True:
        state = check(sha, repo)
        if state in ("success", "failure") or time.monotonic() >= deadline:
            return state
        sleep(CHECK_POLL_SEC)


def merge(number: int, sha: str, method: str, repo: str) -> str:
    """Merge the pull request at exactly ``sha``; the commit main now points to."""
    return api("PUT", f"repos/{repo}/pulls/{int(number)}/merge", {"sha": sha, "merge_method": method})["sha"]


def open_pull(branch: str, title: str, body: str, repo: str) -> Dict[str, Any]:
    """The open pull request from ``branch`` into main, opened now unless one already is."""
    owner = repo.split("/")[0]
    found = api("GET", f"repos/{repo}/pulls?state=open&head={owner}:{branch}")
    pr = found[0] if found else api("POST", f"repos/{repo}/pulls",
                                    {"head": branch, "base": "main", "title": title, "body": body})
    return {"number": pr["number"], "url": pr["html_url"]}


def land(live: Path, commit: str, branch: str, title: str, body: str, *, repo: str, method: str,
         sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Push ``commit`` as ``branch``, open its pull request, and merge it once the test check passed."""
    with workspace.github_auth() as env:
        workspace.git("push", "--quiet", "--force", workspace.remote_url(repo), f"{commit}:refs/heads/{branch}",
                      cwd=live, env=env)
    pr = open_pull(branch, title, body, repo)
    state = wait_check(commit, repo, sleep=sleep)
    if state != "success":
        return {"status": "blocked", "pr": pr, "check": state}
    return {"status": "merged", "pr": pr, "merge": merge(pr["number"], commit, method, repo)}


def protect_main(repo: str) -> Dict[str, Any]:
    """main only through pull requests whose test check passed, for everyone, admins and RMP included."""
    return api("PUT", f"repos/{repo}/branches/main/protection", {
        "required_status_checks": {"strict": False, "checks": [{"context": CHECK}]},
        "enforce_admins": True,
        "required_pull_request_reviews": {"required_approving_review_count": 0},
        "restrictions": None,
        "allow_force_pushes": False,
        "allow_deletions": False,
    })
