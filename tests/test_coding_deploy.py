"""Shipping approved changes against hermetic repos: the live repo, its GitHub remote, the review repository, mirrors.

git runs for real; the host's systemctl, pip, checks and the gate's probes are a recording fake, and so is the
launcher's systemd-run.
"""
import asyncio
import fcntl
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.coding import deploy, verify, workspace
from app.production import canary_sentinel

ROOT = Path(__file__).resolve().parents[1]
BASE = {"app/a.py": "A = 1\n", "worker.py": "W = 1\n", "requirements.txt": "x==1\n", "plugins/p/index.js": "v1\n",
        "plugins/p/old.js": "gone soon\n", "web-stack/backends/w.py": "W = 1\n",
        "ops/systemd/aura-x.service": "[Service]\n", ".cursor/rules/r.mdc": "rule v1\n", "docs/d.md": "docs\n"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def write(root: Path, files: dict, deletes=()):
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    for path in deletes:
        (root / path).unlink()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    live, origin, review, scratch = tmp_path / "live", tmp_path / "origin.git", tmp_path / "review.git", tmp_path / "scratch"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(live))
    write(live, BASE)
    old = commit(live, "base")
    git(live, "remote", "add", "origin", str(origin))
    git(live, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", "--bare", str(live), str(review))
    mirrors = []
    for prefix in ("plugins/", "web-stack/", ".cursor/rules/"):
        target = tmp_path / "mirror" / prefix.strip("/").replace("/", "_")
        for path, text in BASE.items():
            if path.startswith(prefix):
                write(target, {path[len(prefix):]: text})
                # The committed mode, whatever the umask: RMP's own test runs use 0077.
                (target / path[len(prefix):]).chmod(0o644)
        mirrors.append((prefix, target))
    monkeypatch.setattr(deploy, "MIRRORS", tuple(mirrors))
    monkeypatch.setattr(deploy, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(deploy, "CODE_RELOAD_LOCK", tmp_path / "code-reload.lock")

    def change(files: dict, deletes=(), on=None) -> str:
        if scratch.exists():
            subprocess.run(["rm", "-rf", str(scratch)], check=True)
        git(tmp_path, "clone", "-q", str(live), str(scratch))
        git(scratch, "checkout", "-q", on or "main")
        write(scratch, files, deletes)
        head = commit(scratch, "the approved change")
        git(scratch, "push", "-q", str(review), f"{head}:refs/aura/head")
        git(live, "fetch", "-q", str(review), f"+{head}:refs/aura/t1")
        return head

    job = workspace.Job(task_id="t1", repo="rmp", branch="aura/t1-x", base=old, source=str(live), checkout=str(tmp_path / "job"),
                        review=str(review), created_at="2026-10-02T00:00:00+00:00")
    return SimpleNamespace(live=live, origin=origin, review=review, old=old, change=change, job=job,
                           mirror=dict(mirrors), tmp=tmp_path)


class FakeHost(deploy.Host):
    """GitHub is the origin repo: a pull request that merges is pushed onto its main as it is.

    The gate's probes answer from ``units`` (the deploy units running), ``held`` (the code-reload lock) and ``busy``
    (the idle counts in turn, then 0; an exception in it is raised); each count is recorded. CI's check on a commit
    answers from ``ci``, by commit, else "success". ``kill_at`` (a systemctl verb and n) kills the unit at its n-th
    systemctl call of that verb: ``deploy.Interrupted`` is raised there, once, before the call is made. ``merged`` is
    GitHub's record of the pull requests that merged, by branch, which a land adds to; hosts can share it."""

    def __init__(self, live, *, busy=(), checks=(), lands=(), units=(), held=False, ci=None, kill_at=None,
                 merged=None):
        super().__init__(live, repo="Hyper-AI-Lab/openclaw-jev")
        self.calls, self.busy, self.results, self.lands = [], list(busy), list(checks), list(lands)
        self.units, self.held, self.ci = list(units), held, dict(ci or {})
        self.kill_at = list(kill_at) if kill_at else None
        self.merged = {} if merged is None else merged

    def active_user_tasks(self):
        count = self.busy.pop(0) if self.busy else 0
        self.calls.append(("count", count))
        if isinstance(count, Exception):
            raise count
        return count

    def deploy_units(self):
        return list(self.units)

    def reload_lock_held(self):
        return self.held

    def systemctl(self, *args):
        if self.kill_at and args[0] == self.kill_at[0]:
            self.kill_at[1] -= 1
            if self.kill_at[1] == 0:
                self.kill_at = None
                raise deploy.Interrupted(f"killed at systemctl {' '.join(args)}")
        self.calls.append(("systemctl", *args))

    def pip_install(self):
        self.calls.append(("pip",))

    def land(self, commit, branch, title, body, method):
        self.calls.append(("land", branch, method))
        pr = {"number": len(self.calls), "url": f"https://github.com/Hyper-AI-Lab/openclaw-jev/pull/{len(self.calls)}"}
        outcome = self.lands.pop(0) if self.lands else "merged"
        if isinstance(outcome, Exception):
            raise outcome
        if outcome != "merged":
            return {"status": "blocked", "pr": pr, "check": outcome}
        git(self.live, "push", "-q", "origin", f"{commit}:refs/heads/main")
        self.merged[branch] = {"commit": commit, "pr": pr, "merge": commit}
        return {"status": "merged", "pr": pr, "merge": commit}

    def merged_pull(self, commit, branch):
        self.calls.append(("merged_pull", branch))
        found = self.merged.get(branch)
        return {"pr": found["pr"], "merge": found["merge"]} if found and found["commit"] == commit else None

    def wait_check(self, sha):
        self.calls.append(("wait_check", sha))
        return self.ci.get(sha, "success")

    def readiness_report(self):
        return {"telemetry": "fail"}

    def checks(self, restarted, baseline, *, ignore=()):
        self.calls.append(("checks", tuple(restarted), tuple(baseline), *([tuple(ignore)] if ignore else [])))
        return self.results.pop(0) if self.results else {"failure": None, "canary": "ok"}


def spec(repos, head):
    return {"task_id": "t1", "old": repos.old, "head": head, "branch": "aura/t1-x", "title": "The approved change",
            "body": "Body"}


def switched(*units):
    """The systemctl calls of a switch window over ``units``: rmp-api stops first, then the others, then all start."""
    others = [unit for unit in units if unit != "rmp-api"]
    return [*([("systemctl", "stop", "rmp-api")] if "rmp-api" in units else []),
            *([("systemctl", "stop", *others)] if others else []), ("systemctl", "start", *units)]


def test_a_reviewed_change_lands_through_a_pull_request_then_syncs_and_restarts_only_what_changed(repos):
    head = repos.change({"app/a.py": "A = 2\n", "plugins/p/index.js": "v2\n", "web-stack/backends/w.py": "W = 2\n",
                         ".cursor/rules/r.mdc": "rule v2\n", "docs/d.md": "more docs\n"}, deletes=["plugins/p/old.js"])
    host = FakeHost(repos.live)
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)

    assert result["status"] == "deployed" and result["pr"]["url"].startswith("https://github.com/")
    assert "health and readiness passed and the canary passed" in result["summary"]
    assert ("land", "aura/t1-x", "squash") in host.calls and result["commits"] == ["the approved change"]
    assert git(repos.live, "rev-parse", "main") == head == git(repos.origin, "rev-parse", "main")
    plugins, web, rules = repos.mirror["plugins/"], repos.mirror["web-stack/"], repos.mirror[".cursor/rules/"]
    assert (plugins / "p" / "index.js").read_text() == "v2\n" and not (plugins / "p" / "old.js").exists()
    assert (web / "backends" / "w.py").read_text() == "W = 2\n" and (rules / "r.mdc").read_text() == "rule v2\n"
    services = ("aura-web-backends", "openclaw-gateway", "rmp-api", "rmp-worker")
    assert [c for c in host.calls if c[0] in ("systemctl", "pip")] == switched(*services)
    assert ("checks", services, ("telemetry",)) in host.calls


def test_requirements_are_installed_before_the_restart_and_unit_files_are_not(repos):
    head = repos.change({"requirements.txt": "x==2\n", "ops/systemd/aura-x.service": "[Service]\nNice=5\n"})
    host = FakeHost(repos.live)
    assert deploy.self_deploy(spec(repos, head), host, log=lambda m: None)["status"] == "deployed"
    assert [c for c in host.calls if c[0] in ("systemctl", "pip")] == [
        ("systemctl", "stop", "rmp-api"), ("systemctl", "stop", "rmp-worker"), ("pip",),
        ("systemctl", "start", "rmp-api", "rmp-worker")]


def test_a_unit_file_change_is_never_installed_or_restarted(repos, monkeypatch):
    target = repos.tmp / "etc-systemd-system"
    target.mkdir()
    monkeypatch.setattr(deploy, "UNIT_TARGET", target)
    head = repos.change({"ops/systemd/aura-x.service": "[Service]\nNice=5\n", "systemd/aura-y.service": "[Service]\n"})
    host = FakeHost(repos.live)
    assert deploy.self_deploy(spec(repos, head), host, log=lambda m: None)["status"] == "deployed"
    assert list(target.iterdir()) == []
    assert not [c for c in host.calls if c[0] == "systemctl"]


def test_a_deploy_that_changes_a_unit_file_names_it(repos):
    head = repos.change({"ops/systemd/aura-x.service": "[Service]\nNice=5\n"})
    result = deploy.self_deploy(spec(repos, head), FakeHost(repos.live), log=lambda m: None)
    assert result["status"] == "deployed" and result["units"] == ["aura-x.service"]
    assert result["summary"].endswith("Files under ops/systemd/ changed (aura-x.service): the deploy does not "
                                      "install them; an operator reviews them, runs ops/install_units.py, then "
                                      "--apply --head with the sha it printed, in an idle window.")

    repos.old = head
    head = repos.change({"ops/systemd/enabled-units.txt": "aura-x.service\n"})
    result = deploy.self_deploy(spec(repos, head), FakeHost(repos.live), log=lambda m: None)
    assert result["status"] == "deployed" and result["units"] == ["enabled-units.txt"]
    assert "(enabled-units.txt)" in result["summary"]

    repos.old = head
    head = repos.change({"ops/systemd/z.service": "[Service]\n", "ops/systemd/a.service.d/10-x.conf": "[Service]\n"},
                        deletes=["ops/systemd/aura-x.service"])
    result = deploy.self_deploy(spec(repos, head), FakeHost(repos.live), log=lambda m: None)
    assert result["status"] == "deployed"
    assert result["units"] == ["a.service.d/10-x.conf", "aura-x.service", "z.service"]

    repos.old = head
    head = repos.change({"ops/systemd/aura-x.service": "[Service]\nNice=10\n"})
    host = FakeHost(repos.live, checks=[{"failure": "health failed", "canary": None},
                                        {"failure": None, "canary": "ok"}])
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)
    assert result["status"] == "rolled_back" and "units" not in result and "ops/systemd/" not in result["summary"]


def test_a_hand_edited_mirror_blocks_the_deploy_before_main_moves(repos):
    (repos.mirror["plugins/"] / "p" / "index.js").write_text("patched by hand\n")
    head = repos.change({"plugins/p/index.js": "v2\n"})
    host = FakeHost(repos.live)
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)

    assert result["status"] == "blocked" and "plugins/p/index.js" in result["summary"]
    assert git(repos.live, "rev-parse", "main") == repos.old == git(repos.origin, "rev-parse", "main")
    assert not [c for c in host.calls if c[0] in ("systemctl", "land")]


def test_a_mirror_that_is_only_behind_is_updated(repos):
    write(repos.live, {"web-stack/backends/w.py": "W = 2\n"})
    repos.old = commit(repos.live, "a change the mirror never got")
    head = repos.change({"web-stack/backends/w.py": "W = 3\n"})
    result = deploy.self_deploy(spec(repos, head), FakeHost(repos.live), log=lambda m: None)
    assert result["status"] == "deployed" and (repos.mirror["web-stack/"] / "backends" / "w.py").read_text() == "W = 3\n"


def test_a_full_reconcile_converges_a_stale_mirror_file_outside_the_change(repos):
    write(repos.live, {"web-stack/backends/w.py": "W = 2\n"})
    repos.old = commit(repos.live, "a change the mirror never got")
    head = repos.change({"app/a.py": "A = 2\n"})
    host = FakeHost(repos.live)
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)
    assert result["status"] == "deployed"
    assert (repos.mirror["web-stack/"] / "backends" / "w.py").read_text() == "W = 2\n"
    assert [c for c in host.calls if c[0] == "systemctl"] == switched("aura-web-backends", "rmp-api", "rmp-worker")


def test_a_hand_edit_in_a_mirror_the_change_does_not_touch_blocks_the_deploy(repos):
    (repos.mirror[".cursor/rules/"] / "r.mdc").write_text("edited by hand\n")
    head = repos.change({"app/a.py": "A = 2\n"})
    host = FakeHost(repos.live)
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)
    assert result["status"] == "blocked" and ".cursor/rules/r.mdc" in result["summary"]
    assert git(repos.live, "rev-parse", "main") == repos.old == git(repos.origin, "rev-parse", "main")
    assert not [c for c in host.calls if c[0] in ("systemctl", "land")]


def test_a_reconcile_restores_a_mirror_files_mode(repos):
    index = repos.mirror["plugins/"] / "p" / "index.js"
    index.chmod(0o600)
    head = repos.change({"app/a.py": "A = 2\n"})
    host = FakeHost(repos.live)
    assert deploy.self_deploy(spec(repos, head), host, log=lambda m: None)["status"] == "deployed"
    assert oct(index.stat().st_mode & 0o777) == oct(0o644) and index.read_text() == "v1\n"
    assert [c for c in host.calls if c[0] == "systemctl"] == switched("openclaw-gateway", "rmp-api", "rmp-worker")


def test_an_executable_mirror_file_lands_at_0755_and_is_then_left_alone(repos):
    write(repos.live, {"web-stack/run-local.sh": "#!/bin/sh\n"})
    (repos.live / "web-stack" / "run-local.sh").chmod(0o755)
    repos.old = commit(repos.live, "an executable the mirror never got")
    head = repos.change({"app/a.py": "A = 2\n"})
    assert deploy.self_deploy(spec(repos, head), FakeHost(repos.live), log=lambda m: None)["status"] == "deployed"
    script = repos.mirror["web-stack/"] / "run-local.sh"
    assert oct(script.stat().st_mode & 0o777) == oct(0o755) and script.read_text() == "#!/bin/sh\n"

    repos.old = head
    head = repos.change({"app/a.py": "A = 3\n"})
    host = FakeHost(repos.live)
    assert deploy.self_deploy(spec(repos, head), host, log=lambda m: None)["status"] == "deployed"
    assert oct(script.stat().st_mode & 0o777) == oct(0o755)
    assert [c for c in host.calls if c[0] == "systemctl"] == switched("rmp-api", "rmp-worker")


def test_a_missing_mirror_file_is_restored_not_refused(repos):
    index = repos.mirror["plugins/"] / "p" / "index.js"
    index.unlink()
    (repos.mirror["plugins/"] / "p" / "notes.txt").write_text("the host's own file\n")
    head = repos.change({"plugins/p/old.js": "still here\n"})
    result = deploy.self_deploy(spec(repos, head), FakeHost(repos.live), log=lambda m: None)
    assert result["status"] == "deployed" and index.read_text() == "v1\n"
    assert (repos.mirror["plugins/"] / "p" / "old.js").read_text() == "still here\n"
    assert (repos.mirror["plugins/"] / "p" / "notes.txt").read_text() == "the host's own file\n"


def test_a_live_copy_git_cannot_read_fails_with_gits_reason(repos):
    with pytest.raises(RuntimeError, match="^git hash-object --no-filters failed: .*absent"):
        deploy.blob_ids(repos.live, [repos.tmp / "absent"])


def test_failed_checks_revert_main_at_once_restore_the_mirrors_restart_and_land_the_revert_by_merge_commit(repos):
    head = repos.change({"app/a.py": "A = 2\n", "plugins/p/index.js": "v2\n"}, deletes=["plugins/p/old.js"])
    host = FakeHost(repos.live, checks=[{"failure": "CANARY FAIL: task failed", "canary": None}, {"failure": None, "canary": "ok"}])
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)

    revert = git(repos.live, "rev-parse", "main")
    assert result["status"] == "rolled_back" and result["revert"] == revert and "Aura is healthy again" in result["summary"]
    assert "GitHub's main is reverted too" in result["summary"] and result["revert_pr"]["status"] == "merged"
    assert ("land", f"aura/revert-{head[:12]}", "merge") in host.calls
    assert git(repos.live, "rev-parse", f"{revert}^{{tree}}") == git(repos.live, "rev-parse", f"{repos.old}^{{tree}}")
    assert git(repos.origin, "rev-parse", "main") == revert
    plugins = repos.mirror["plugins/"]
    assert (plugins / "p" / "index.js").read_text() == "v1\n" and (plugins / "p" / "old.js").read_text() == "gone soon\n"
    assert [c for c in host.calls if c[0] == "systemctl"] == switched("openclaw-gateway", "rmp-api", "rmp-worker") * 2


def test_main_moving_while_the_deploy_waited_deploys_nothing(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    write(repos.live, {"docs/d.md": "another deploy\n"})
    moved = commit(repos.live, "another deploy")
    result = deploy.self_deploy(spec(repos, head), FakeHost(repos.live), log=lambda m: None)
    assert result["status"] == "failed" and "main moved" in result["summary"] and git(repos.live, "rev-parse", "main") == moved


def main_spec(head):
    return {"task_id": deploy.MAIN, "source": "github", "head": head, "ci": "success"}


def test_a_deploy_of_githubs_main_ships_the_commit_ci_passed_and_then_finds_nothing_new(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    git(repos.live, "push", "-q", "origin", f"{head}:refs/heads/main")
    host = FakeHost(repos.live)
    result = deploy.self_deploy(main_spec(head), host, log=lambda m: None)
    assert result["status"] == "deployed" and result["commits"] == ["the approved change"] and "pr" not in result
    assert result["units"] == [] and "ops/systemd/" not in result["summary"]
    assert git(repos.live, "rev-parse", "main") == head and not [c for c in host.calls if c[0] == "land"]
    assert [c for c in host.calls if c[0] == "systemctl"] == switched("rmp-api", "rmp-worker")

    again = deploy.self_deploy(main_spec(head), FakeHost(repos.live), log=lambda m: None)
    assert again["status"] == "unchanged" and "already live" in again["summary"] and again["restarted"] == []


def test_a_live_main_with_commits_github_lacks_is_never_deployed_over(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    git(repos.live, "push", "-q", "origin", f"{head}:refs/heads/main")
    write(repos.live, {"docs/d.md": "edited on the server\n"})
    local = commit(repos.live, "a commit only the live repo has")
    result = deploy.self_deploy(main_spec(head), FakeHost(repos.live), log=lambda m: None)
    assert result["status"] == "blocked" and "lacks" in result["summary"] and git(repos.live, "rev-parse", "main") == local


def test_a_hand_edit_in_any_mirror_blocks_a_deploy_of_githubs_main(repos):
    rule = repos.mirror[".cursor/rules/"] / "r.mdc"
    rule.write_text("edited by hand\n")
    head = repos.change({"app/a.py": "A = 2\n"})
    git(repos.live, "push", "-q", "origin", f"{head}:refs/heads/main")
    host = FakeHost(repos.live)
    result = deploy.self_deploy(main_spec(head), host, log=lambda m: None)
    assert result["status"] == "blocked" and ".cursor/rules/r.mdc" in result["summary"]
    assert git(repos.live, "rev-parse", "main") == repos.old and rule.read_text() == "edited by hand\n"
    assert not [c for c in host.calls if c[0] == "systemctl"]


def test_a_reviewed_change_whose_pull_request_fails_ci_deploys_nothing(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    host = FakeHost(repos.live, lands=["failure"])
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)
    assert result["status"] == "blocked" and "is failure" in result["summary"] and result["pr"]["url"]
    assert git(repos.live, "rev-parse", "main") == repos.old == git(repos.origin, "rev-parse", "main")
    assert not [c for c in host.calls if c[0] == "systemctl"]


def test_a_rollback_whose_revert_cannot_reach_github_says_so(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    host = FakeHost(repos.live, checks=[{"failure": "health failed", "canary": None}, {"failure": None, "canary": "ok"}],
                    lands=["merged", RuntimeError("GitHub is down")])
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)
    assert result["status"] == "rolled_back" and "the revert is not on GitHub yet (GitHub is down)" in result["summary"]
    assert git(repos.origin, "rev-parse", "main") == head != git(repos.live, "rev-parse", "main")


def test_the_approved_commit_must_fast_forward_main(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    assert deploy.fetch_approved(repos.job, head, live=repos.live) == {"status": "ready", "old": repos.old}
    assert git(repos.live, "rev-parse", "refs/aura/t1") == head
    write(repos.live, {"docs/d.md": "another deploy\n"})
    moved = commit(repos.live, "another deploy")
    assert deploy.fetch_approved(repos.job, head, live=repos.live) == {"status": "needs_rebase", "main": moved}


def test_a_rebase_gets_todays_main_in_a_bundle_and_as_the_review_base(repos, monkeypatch):
    monkeypatch.setattr(deploy, "BUNDLES_DIR", repos.tmp / "bundles")
    monkeypatch.setattr(workspace, "RUNS_DIR", repos.tmp / "runs")
    workspace.job_file("t1").parent.mkdir(parents=True)
    write(repos.live, {"docs/d.md": "another deploy\n"})
    moved = commit(repos.live, "another deploy")

    job = deploy.refresh_base(repos.job, live=repos.live)
    bundle = repos.tmp / "bundles" / "t1" / "main.bundle"
    assert job.base == moved and json.loads(workspace.job_file("t1").read_text())["base"] == moved
    assert moved in git(repos.tmp, "bundle", "list-heads", str(bundle)) and oct(bundle.stat().st_mode)[-3:] == "644"
    assert git(repos.review, "rev-parse", "refs/aura/base") == moved


def test_a_pull_request_pushes_the_branch_and_opens_the_pr_with_the_token(repos, monkeypatch):
    github = repos.tmp / "github.git"
    git(repos.tmp, "init", "-q", "--bare", str(github))
    token = repos.tmp / "github_pat"
    token.write_text("fake-token\n")
    seen = repos.tmp / "gh.json"
    gh = repos.tmp / "gh"
    gh.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                  f"json.dump({{'argv': sys.argv[1:], 'token': os.environ.get('GH_TOKEN')}}, open({str(seen)!r}, 'w'))\n"
                  "print('https://github.com/Hyper-AI-Lab/agentic-design/pull/7')\n")
    gh.chmod(0o755)
    monkeypatch.setattr(workspace, "remote_url", lambda remote: str(github))
    monkeypatch.setattr(workspace, "TOKEN_FILE", token)
    head = repos.change({"app/a.py": "A = 2\n"})
    entry = {"remote": "Hyper-AI-Lab/agentic-design", "branch": "main"}

    result = deploy.open_pull_request(repos.job, head, entry, "Fix the greeting", "Body", gh=str(gh))
    assert result["status"] == "pr_opened" and result["url"].endswith("/pull/7")
    assert git(github, "rev-parse", "refs/heads/aura/t1-x") == head
    called = json.loads(seen.read_text())
    assert called["token"] == "fake-token"
    assert called["argv"][:2] == ["pr", "create"] and ["--head", "aura/t1-x"] == called["argv"][4:6]

    gh.write_text("#!/bin/sh\necho 'a pull request for branch aura/t1-x already exists' >&2\nexit 1\n")
    failed = deploy.open_pull_request(repos.job, head, entry, "Fix the greeting", "Body", gh=str(gh))
    assert failed["status"] == "failed" and "already exists" in failed["summary"]


@pytest.fixture
def systemd_run(monkeypatch):
    """systemd-run and systemctl's unit list and stop as the launcher and an abort call them: each start's argv is
    recorded, or raises while ``fail`` is set, ``units`` are the deploy units, listed when the list asks for their
    state (a name is active; a (name, state) pair has that state), and each unit stopped is recorded in ``stopped``,
    after which ``on_stop`` runs (the unit's own end on the stop's SIGTERM). Every other command runs for real."""
    real, state = subprocess.run, SimpleNamespace(argvs=[], fail=False, units=[], stopped=[], on_stop=None)

    def run(argv, **kwargs):
        if argv[:2] == ["systemctl", "list-units"]:
            wanted = next(arg for arg in argv if arg.startswith("--state=")).split("=", 1)[1].split(",")
            units = [(unit, "active") if isinstance(unit, str) else unit for unit in state.units]
            return SimpleNamespace(returncode=0, stderr="", stdout="".join(
                f"{unit} loaded {active} running Deploy\n" for unit, active in units if active in wanted))
        if argv[:2] == ["systemctl", "stop"]:
            state.stopped += argv[2:]
            if state.on_stop:
                state.on_stop()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:1] != ["systemd-run"]:
            return real(argv, **kwargs)
        if state.fail:
            raise subprocess.CalledProcessError(1, argv)
        state.argvs.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    return state


def test_the_hand_off_queues_once_and_launches_when_the_gate_is_open(repos, systemd_run, monkeypatch):
    lock = repos.tmp / "code-reload.lock"
    monkeypatch.setattr(deploy, "CODE_RELOAD_LOCK", lock)
    counts = []
    monkeypatch.setattr(canary_sentinel, "count_active_user_tasks_sync", lambda *, strict=False: counts.append(strict) or 0)
    the_spec = {"task_id": "t1", "old": repos.old, "head": "c" * 40}
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert deploy.hand_off(the_spec, live=repos.live) == "aura-deploy-t1"
    assert systemd_run.argvs == [] and counts == []
    assert deploy.hand_off(the_spec, live=repos.live) == "aura-deploy-t1" and counts == [True]
    systemd_run.units = ["aura-deploy-t1.service"]
    assert deploy.hand_off(the_spec, live=repos.live) == "aura-deploy-t1" and len(systemd_run.argvs) == 1
    argv = systemd_run.argvs[0]
    assert argv[:2] == ["systemd-run", "--unit=aura-deploy-t1"] and not any(a.startswith("--uid") for a in argv)
    assert "--property=RuntimeMaxSec=10800" in argv
    assert "--property=EnvironmentFile=-/etc/rmp/rmp.env" in argv and argv[-2:] == [
        str(repos.live / "ops" / "coding_deploy.py"), str(deploy.spec_file("t1"))]
    assert json.loads(deploy.spec_file("t1").read_text())["head"] == "c" * 40
    run = json.loads(deploy.state_file("t1").read_text())
    assert {key: run[key] for key in ("phase", "attempt", "launches")} == {"phase": "queued", "attempt": 0, "launches": 1}
    assert oct(deploy.spec_file("t1").parent.stat().st_mode & 0o777) == oct(0o700)


def test_a_failed_launch_from_the_hand_off_leaves_the_run_queued(repos, systemd_run, monkeypatch):
    host = FakeHost(repos.live)
    monkeypatch.setattr(deploy, "Host", lambda live: host)
    systemd_run.fail = True
    assert deploy.hand_off({"task_id": "t1", "old": repos.old, "head": "c" * 40}, live=repos.live) == "aura-deploy-t1"
    run = json.loads(deploy.state_file("t1").read_text())
    assert run["phase"] == "queued" and run["launches"] == 0 and systemd_run.argvs == []
    systemd_run.fail = False
    assert deploy.next_deploy(host) == "aura-deploy-t1" and len(systemd_run.argvs) == 1
    assert json.loads(deploy.state_file("t1").read_text())["launches"] == 1


def test_a_queued_run_takes_a_new_head_and_a_run_past_queued_keeps_its_own(repos):
    deploy.queue({"task_id": "t1", "old": repos.old, "head": "c" * 40})
    queued = deploy.state_file("t1").read_text()
    assert deploy.queue({"task_id": "t1", "old": repos.old, "head": "c" * 40}) == "aura-deploy-t1"
    assert deploy.state_file("t1").read_text() == queued
    deploy.queue({"task_id": "t1", "old": repos.old, "head": "d" * 40})
    assert json.loads(deploy.spec_file("t1").read_text())["head"] == "d" * 40
    run = json.loads(deploy.state_file("t1").read_text())
    deploy.write_json(deploy.state_file("t1"), {**run, "phase": "landed"})
    with pytest.raises(RuntimeError, match="^a deploy of t1 is in progress$"):
        deploy.queue({"task_id": "t1", "old": repos.old, "head": "e" * 40})
    assert deploy.queue({"task_id": "t1", "old": repos.old, "head": "d" * 40}) == "aura-deploy-t1"
    assert json.loads(deploy.spec_file("t1").read_text())["head"] == "d" * 40

    deploy.state_file("t1").unlink()
    assert deploy.queue({"task_id": "t1", "old": repos.old, "head": "d" * 40}) == "aura-deploy-t1"
    assert not deploy.state_file("t1").exists()


def test_a_deploy_of_githubs_main_starts_once_while_one_is_waiting(repos, github_ci, systemd_run):
    first = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    host = FakeHost(repos.live)
    assert deploy.watch_main(host) == "aura-deploy-main"
    host.units = ["aura-deploy-main.service"]
    later = repos.change({"app/a.py": "A = 3\n"}, on=first)
    git(repos.live, "push", "-q", "origin", f"{later}:refs/heads/main")
    assert deploy.watch_main(host) is None and len(systemd_run.argvs) == 1
    host.units = []
    assert deploy.watch_main(host) == "aura-deploy-main" and len(systemd_run.argvs) == 2
    assert json.loads(deploy.spec_file(deploy.MAIN).read_text())["head"] == later


def test_the_exact_commit_suite_runs_on_a_clean_checkout_of_the_head(repos, monkeypatch):
    monkeypatch.setattr(deploy, "JOBS_DIR", repos.tmp / "jobs")
    (repos.tmp / "jobs").mkdir()
    head = repos.change({"app/a.py": "A = 2\n"})
    seen = {}

    def run_tests(job, cfg, *, attempt):
        checkout = Path(job.checkout)
        seen.update(head=git(checkout, "rev-parse", "HEAD"), a=(checkout / "app" / "a.py").read_text(), attempt=attempt,
                    clean=git(checkout, "status", "--porcelain"))
        return {"ok": True, "commands": []}

    monkeypatch.setattr(verify, "run_tests", run_tests)
    assert deploy.exact_commit_suite(repos.job, head, {}, owner=None) == {"ok": True, "commands": []}
    assert seen == {"head": head, "a": "A = 2\n", "attempt": "deploy", "clean": ""}
    assert not (repos.tmp / "jobs" / "t1-deploy").exists()


def test_the_deploy_unit_holds_the_lock_records_the_result_and_starts_the_reply_run(repos, monkeypatch):
    module_spec = importlib.util.spec_from_file_location("coding_deploy", ROOT / "ops" / "coding_deploy.py")
    unit = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(unit)
    held = []

    class LockProbingHost(FakeHost):
        """Notes, at each systemctl call, whether another process could take the code-reload lock."""

        def systemctl(self, *args):
            with open(deploy.CODE_RELOAD_LOCK, "w") as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                except BlockingIOError:
                    held.append(True)
            super().systemctl(*args)

    monkeypatch.setattr(unit.deploy, "Host", lambda: LockProbingHost(repos.live))
    added, recorded, written = [], [], []
    write_json = deploy.write_json

    def recording_write_json(path, data):
        """Each file written atomically notes whether the run's state was still there."""
        written.append((path.name, deploy.state_file("t1").exists()))
        write_json(path, data)

    monkeypatch.setattr(unit.deploy, "write_json", recording_write_json)

    def add(event):
        # finish runs once the result is recorded and the run's state is gone.
        recorded.append((path.with_name("result.json").exists(), deploy.state_file("t1").exists()))
        added.append(event)

    db = SimpleNamespace(add=add, get=AsyncMock(return_value=SimpleNamespace(supplementary_context={"a": 1})),
                         commit=AsyncMock())
    session = AsyncMock()
    session.__aenter__.return_value = db
    monkeypatch.setattr(unit, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(unit, "engine", SimpleNamespace(dispose=AsyncMock()))
    client = SimpleNamespace(start_workflow=AsyncMock())
    monkeypatch.setattr(unit, "connect_temporal", AsyncMock(return_value=client))
    head = repos.change({"app/a.py": "A = 2\n"})
    deploy.queue(reviewed(repos, head=head))
    path = deploy.spec_file("t1")

    assert unit.main(str(path)) == 0 and held == [True] * 3
    # The result is written atomically while the state is still there, so a unit that dies between the two loses no
    # run.
    assert ("result.json", True) in written and written[-1] == ("result.json", True)
    assert recorded == [(True, False)] and not deploy.state_file("t1").exists()
    assert added[0].event_type == "coding.deploy" and added[0].event_payload["status"] == "deployed"
    name, payload = client.start_workflow.await_args.args
    assert name == "CodingTaskWorkflow" and client.start_workflow.await_args.kwargs["id"] == "workflow-t1"
    assert payload["task_id"] == "t1" and payload["report"]["shipped"]["status"] == "deployed"
    assert db.get.return_value.supplementary_context == {"a": 1, "coding_deploy": payload["report"]["shipped"]}


def deploy_unit(monkeypatch):
    """``ops/coding_deploy.py`` with its database, Slack and Temporal faked: the module, and what it recorded
    (``added``, ``db``), told Kirill (``notes``) and started (``client``)."""
    module_spec = importlib.util.spec_from_file_location("coding_deploy", ROOT / "ops" / "coding_deploy.py")
    unit = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(unit)
    seen = SimpleNamespace(added=[], notes=[], client=SimpleNamespace(start_workflow=AsyncMock()))
    seen.db = SimpleNamespace(add=seen.added.append, get=AsyncMock(return_value=None), commit=AsyncMock())
    session = AsyncMock()
    session.__aenter__.return_value = seen.db
    monkeypatch.setattr(unit, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(unit, "engine", SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(unit, "get_coding_config", lambda: {"repositories": {"rmp": {"remote": "Hyper-AI-Lab/openclaw-jev"}}})

    async def notify(message, *, incident_id):
        seen.notes.append((message, incident_id))
        return True

    monkeypatch.setattr(unit, "notify_ops_slack", notify)
    monkeypatch.setattr(unit, "connect_temporal", AsyncMock(return_value=seen.client))
    return unit, seen


def run_github_unit(tmp_path, monkeypatch, status, **fields):
    """Run the deploy unit for a deploy of GitHub's main that ends in ``status``, with any further result ``fields``;
    what it recorded, said and started."""
    unit, seen = deploy_unit(monkeypatch)
    monkeypatch.setattr(unit.deploy, "CODE_RELOAD_LOCK", tmp_path / "code-reload.lock")
    monkeypatch.setattr(unit.deploy, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(unit.deploy, "self_deploy", lambda the_spec, host, *, log: {
        "status": status, "head": "c" * 40, "commits": ["Fix the greeting (#12)"], "summary": "Deployed cccccccccccc.",
        **fields})
    monkeypatch.setattr(unit.deploy, "Host", lambda: FakeHost(tmp_path))
    deploy.queue({"task_id": "main", "source": "github", "head": "c" * 40, "ci": "success"})
    path = deploy.spec_file("main")

    code = unit.main(str(path))
    added = seen.added
    assert added[0].event_type == "coding.deploy" and added[0].entity_type == "deploy" and added[0].entity_id == "main"
    assert added[0].event_payload["status"] == status and added[0].event_payload["ci"] == "success"
    assert seen.db.commit.await_count == 1
    assert json.loads(path.with_name("result.json").read_text())["head"] == "c" * 40
    assert not deploy.state_file("main").exists()
    seen.client.start_workflow.assert_not_awaited()
    return code, seen.notes


@pytest.mark.parametrize("status, code, fields", [("deployed", 0, {}), ("deployed", 0, {"units": []}),
                                                  ("unchanged", 1, {})])
def test_a_deploy_of_githubs_main_that_is_live_is_recorded_without_telling_kirill(tmp_path, monkeypatch, status, code,
                                                                                  fields):
    assert run_github_unit(tmp_path, monkeypatch, status, **fields) == (code, [])


def test_a_live_deploy_that_changed_unit_files_still_tells_kirill(tmp_path, monkeypatch):
    summary = ("Deployed cccccccccccc. Files under ops/systemd/ changed (aura-x.service): the deploy does not install "
               "them; an operator reviews them, runs ops/install_units.py, then --apply --head with the sha it "
               "printed, in an idle window.")
    code, notes = run_github_unit(tmp_path, monkeypatch, "deployed", units=["aura-x.service"], summary=summary)
    assert code == 0 and len(notes) == 1
    note, incident = notes[0]
    assert note.startswith("Aura's change is live.") and "aura-x.service" in note
    assert incident == "deploy:main:cccccccccccc"


@pytest.mark.parametrize("status, headline", [
    ("rolled_back", "Aura's change failed its checks and was rolled back."),
    ("blocked", "Aura's deploy: blocked."), ("failed", "Aura's deploy: failed."),
    ("postponed", "Aura's deploy: postponed.")])
def test_a_deploy_of_githubs_main_that_is_not_live_still_tells_kirill(tmp_path, monkeypatch, status, headline):
    code, [(note, incident)] = run_github_unit(tmp_path, monkeypatch, status)
    assert code == 1 and note.startswith(headline) and "https://github.com/Hyper-AI-Lab/openclaw-jev/pull/12" in note
    assert incident == "deploy:main:cccccccccccc"


@pytest.fixture
def github_ci(monkeypatch):
    """CI's test check on GitHub's main (``check``); whether the deploy unit of GitHub's main runs is the host's."""
    state = SimpleNamespace(check="success")
    monkeypatch.setattr(deploy.github, "check", lambda sha, repo: state.check)
    return state


@pytest.fixture
def watching(github_ci, monkeypatch):
    """The watcher with a launcher that records, in ``started``, the spec of GitHub's main whenever its run is queued."""
    github_ci.started = []

    def next_deploy(host):
        if not deploy.state_file(deploy.MAIN).exists():
            return None
        github_ci.started.append(json.loads(deploy.spec_file(deploy.MAIN).read_text()))
        return f"aura-deploy-{deploy.MAIN}"

    monkeypatch.setattr(deploy, "next_deploy", next_deploy)
    return github_ci


def merged_on_github(repos, files):
    head = repos.change(files)
    git(repos.live, "push", "-q", "origin", f"{head}:refs/heads/main")
    return head


def test_the_watcher_deploys_githubs_main_once_ci_passed_on_that_commit(repos, watching):
    host = FakeHost(repos.live)
    assert deploy.watch_main(host) is None and watching.started == []
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    for check in ("missing", "pending", "failure"):
        watching.check = check
        assert deploy.watch_main(host) is None
    watching.check, host.units = "success", ["aura-deploy-main.service"]
    assert deploy.watch_main(host) is None
    host.units = []
    assert deploy.watch_main(host) == "aura-deploy-main"
    assert watching.started == [{"task_id": "main", "source": "github", "head": head, "ci": "success"}]


def test_the_watcher_does_not_retry_a_commit_that_was_blocked_or_rolled_back(repos, watching):
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    result = deploy.spec_file(deploy.MAIN).with_name("result.json")
    result.parent.mkdir(parents=True)
    for status in deploy.NOT_RETRIED:
        result.write_text(json.dumps({"head": head, "status": status}))
        assert deploy.watch_main(FakeHost(repos.live)) is None
    result.write_text(json.dumps({"head": head, "status": "postponed"}))
    assert deploy.watch_main(FakeHost(repos.live)) == "aura-deploy-main"


def test_the_watcher_never_deploys_over_a_live_main_with_commits_github_lacks(repos, watching):
    merged_on_github(repos, {"app/a.py": "A = 2\n"})
    write(repos.live, {"docs/d.md": "edited on the server\n"})
    commit(repos.live, "a commit only the live repo has")
    assert deploy.watch_main(FakeHost(repos.live)) is None and watching.started == []


def test_pending_main_says_how_long_githubs_main_has_waited(repos, monkeypatch):
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    monkeypatch.setattr(deploy.github, "main_sha", lambda repo: head)
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(deploy.github, "api", lambda method, path, body=None: {"commit": {"committer": {"date": two_hours_ago}}})
    assert deploy.pending_main("x/y", live=repos.live) == {"head": head, "age_hours": 2.0}
    git(repos.live, "merge", "-q", "--ff-only", head)
    assert deploy.pending_main("x/y", live=repos.live) is None


def test_every_file_of_the_rmp_adapter_plugin_restarts_the_gateway_when_it_changes():
    # The gateway loads plugin code and manifests at start, so a change to any file here (the exec
    # result cap, its manifest contract, index.js) only takes effect after a restart of the gateway.
    plugin_files = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "plugins" / "rmp_adapter").rglob("*")
        if path.is_file() and "node_modules" not in path.parts
    )
    assert "plugins/rmp_adapter/exec_result_cap.js" in plugin_files
    assert "plugins/rmp_adapter/openclaw.plugin.json" in plugin_files
    for file in plugin_files:
        assert deploy.restarts_for([file]) == ["openclaw-gateway"], file
    assert deploy.restarts_for(plugin_files) == ["openclaw-gateway"]


def test_a_deploy_that_only_adds_a_plugin_file_restarts_only_the_gateway(repos):
    head = repos.change({"plugins/p/exec_cap.js": "module.exports = {};\n"})
    host = FakeHost(repos.live)
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)

    assert result["status"] == "deployed"
    assert (repos.mirror["plugins/"] / "p" / "exec_cap.js").read_text() == "module.exports = {};\n"
    assert [c for c in host.calls if c[0] == "systemctl"] == switched("openclaw-gateway")


def reviewed(repos, task="t1", head="c" * 40):
    """A reviewed job's deploy spec with what the unit's reply run needs."""
    context = {"task_id": task, "session_key": "s", "intent": "Fix it", "task_type": "user", "tags": []}
    return {"task_id": task, "old": repos.old, "head": head, "branch": f"aura/{task}-x", "title": "The approved change",
            "body": "Body", "context": context, "report": {"brief": {}, "review": {}, "evidence": {}}}


def queued_for(task, seconds):
    """Move the queued run of ``task`` back by ``seconds``."""
    run = json.loads(deploy.state_file(task).read_text())
    run["queued_at"] = (datetime.fromisoformat(run["queued_at"]) - timedelta(seconds=seconds)).isoformat()
    deploy.write_json(deploy.state_file(task), run)


def test_githubs_main_is_queued_and_launched_only_through_the_gate(repos, github_ci, systemd_run):
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    host = FakeHost(repos.live, busy=[1, 1])
    assert deploy.gate(host) == ("1 active user task(s)", True)
    assert deploy.watch_main(host) is None and systemd_run.argvs == []
    assert json.loads(deploy.spec_file(deploy.MAIN).read_text()) == main_spec(head)
    assert json.loads(deploy.state_file(deploy.MAIN).read_text())["phase"] == "queued"

    host.held = True
    assert deploy.gate(host) == ("the code-reload lock is held", False)
    assert deploy.watch_main(host) is None and systemd_run.argvs == []
    host.held, host.units = False, ["aura-deploy-t9.service"]
    assert deploy.gate(host) == ("a deploy unit is running", False)
    assert deploy.watch_main(host) is None and systemd_run.argvs == []
    host.units = []
    assert deploy.watch_main(host) == "aura-deploy-main" and len(systemd_run.argvs) == 1
    assert systemd_run.argvs[0][1] == "--unit=aura-deploy-main"
    assert systemd_run.argvs[0][-1] == str(deploy.spec_file(deploy.MAIN))
    assert json.loads(deploy.state_file(deploy.MAIN).read_text())["launches"] == 1


def test_an_idle_count_that_fails_keeps_the_deploy_queued(repos, github_ci, systemd_run):
    merged_on_github(repos, {"app/a.py": "A = 2\n"})
    host = FakeHost(repos.live, busy=[RuntimeError("could not connect to server"), RuntimeError("timeout")])
    assert deploy.gate(host) == ("the idle count failed: could not connect to server", False)
    assert deploy.watch_main(host) is None and systemd_run.argvs == []
    assert json.loads(deploy.state_file(deploy.MAIN).read_text())["phase"] == "queued"
    assert deploy.watch_main(host) == "aura-deploy-main" and len(systemd_run.argvs) == 1


def test_a_reviewed_run_launches_before_githubs_main(repos, github_ci, systemd_run):
    deploy.queue(main_spec("a" * 40))
    deploy.queue(reviewed(repos, "t2"))
    deploy.queue(reviewed(repos, "t1"))
    host = FakeHost(repos.live)
    # The reconciler's pass launches queued runs even when GitHub's main has not moved.
    assert deploy.watch_main(host) == "aura-deploy-t2"
    host.units = ["aura-deploy-t2.service"]
    assert deploy.next_deploy(host) is None
    host.units = []
    deploy.state_file("t2").unlink()
    assert deploy.next_deploy(host) == "aura-deploy-t1"
    deploy.state_file("t1").unlink()
    assert deploy.next_deploy(host) == "aura-deploy-main"
    assert [argv[1] for argv in systemd_run.argvs] == [
        "--unit=aura-deploy-t2", "--unit=aura-deploy-t1", "--unit=aura-deploy-main"]


def test_the_reconcilers_pass_launches_queued_runs_while_github_cannot_be_read(repos, github_ci, systemd_run):
    host = FakeHost(repos.live)

    def fetch_main():
        raise RuntimeError("git fetch failed: Could not resolve host: github.com")

    host.fetch_main = fetch_main
    deploy.queue(reviewed(repos))
    with pytest.raises(RuntimeError, match="Could not resolve host"):
        deploy.watch_main(host)
    assert [argv[1] for argv in systemd_run.argvs] == ["--unit=aura-deploy-t1"]

    deploy.state_file("t1").unlink()
    deploy.queue(reviewed(repos, "t2"))
    queued_for("t2", deploy.IDLE_WAIT_SEC)
    host.busy = [1]
    with pytest.raises(RuntimeError, match="Could not resolve host"):
        deploy.watch_main(host)
    assert systemd_run.argvs[-1][1] == "--unit=aura-deploy-t2" and systemd_run.argvs[-1][-1] == "--postpone"


def test_a_run_queued_for_two_hours_is_postponed(repos, github_ci, systemd_run, monkeypatch, capsys):
    unit, seen = deploy_unit(monkeypatch)
    lock = repos.tmp / "code-reload.lock"
    monkeypatch.setattr(deploy, "CODE_RELOAD_LOCK", lock)
    monkeypatch.setattr(deploy, "self_deploy", lambda *args, **kwargs: pytest.fail("a postponed run deployed"))
    busy = FakeHost(repos.live, busy=[1] * 10)

    deploy.queue(reviewed(repos))
    queued_for("t1", deploy.IDLE_WAIT_SEC - 60)
    assert deploy.next_deploy(busy) is None and systemd_run.argvs == []
    queued_for("t1", 60)
    # The postponed launch needs a busy gate and never starts while another deploy unit runs.
    busy.units = ["aura-deploy-t9.service"]
    assert deploy.next_deploy(busy) is None and systemd_run.argvs == []
    busy.units = []
    assert deploy.next_deploy(busy) == "aura-deploy-t1"
    assert systemd_run.argvs[-1][-2:] == [str(deploy.spec_file("t1")), "--postpone"]
    capsys.readouterr()
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert unit.main(str(deploy.spec_file("t1")), postpone=True) == 0
    assert capsys.readouterr().out.splitlines()[0].endswith(
        " launch 1 of the deploy of cccccccccccc (phase queued, postpone)")
    assert json.loads(deploy.state_file("t1").read_text())["phase"] == "queued"
    assert not deploy.spec_file("t1").with_name("result.json").exists() and seen.added == []
    assert unit.main(str(deploy.spec_file("t1")), postpone=True) == 1
    result = json.loads(deploy.spec_file("t1").with_name("result.json").read_text())
    assert result["status"] == "postponed" and result["head"] == "c" * 40
    assert result["summary"] == ("Aura was still busy 2 hours after this deploy was queued, so nothing was deployed; "
                                 "main is unchanged.")
    assert not deploy.state_file("t1").exists() and seen.notes == []
    name, payload = seen.client.start_workflow.await_args.args
    assert name == "CodingTaskWorkflow" and payload["report"]["shipped"]["status"] == "postponed"

    deploy.queue(reviewed(repos, "t2"))
    deploy.write_json(deploy.state_file("t2"), {**json.loads(deploy.state_file("t2").read_text()), "phase": "landed"})
    queued_for("t2", deploy.IDLE_WAIT_SEC)
    assert deploy.next_deploy(busy) == "aura-deploy-t2"
    assert unit.main(str(deploy.spec_file("t2")), postpone=True) == 1
    assert json.loads(deploy.spec_file("t2").with_name("result.json").read_text())["summary"] == (
        "Aura was still busy 2 hours after this deploy was queued, so nothing was deployed here; GitHub's main has the "
        "change, and it goes live with GitHub's main once Aura is idle.")

    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    assert deploy.watch_main(busy) is None
    queued_for(deploy.MAIN, deploy.IDLE_WAIT_SEC)
    assert deploy.watch_main(busy) == "aura-deploy-main" and systemd_run.argvs[-1][-1] == "--postpone"
    assert unit.main(str(deploy.spec_file(deploy.MAIN)), postpone=True) == 1
    [(note, incident)] = seen.notes
    assert note.startswith("Aura's deploy: postponed.") and incident == f"deploy:main:{head[:12]}"
    assert not deploy.state_file(deploy.MAIN).exists()
    launches = len(systemd_run.argvs)
    assert deploy.watch_main(busy) is None and len(systemd_run.argvs) == launches
    run = json.loads(deploy.state_file(deploy.MAIN).read_text())
    assert run["phase"] == "queued" and run["launches"] == 0
    assert json.loads(deploy.spec_file(deploy.MAIN).read_text())["head"] == head


def test_a_run_that_waited_behind_another_unit_deploys_when_the_gate_opens(repos, systemd_run):
    deploy.queue(reviewed(repos))
    queued_for("t1", deploy.IDLE_WAIT_SEC)
    host = FakeHost(repos.live, units=["aura-deploy-t9.service"])
    assert deploy.next_deploy(host) is None and systemd_run.argvs == []
    host.units = []
    assert deploy.next_deploy(host) == "aura-deploy-t1"
    assert [argv[-1] for argv in systemd_run.argvs] == [str(deploy.spec_file("t1"))]


def test_a_held_reload_lock_postpones_nothing(repos, systemd_run):
    deploy.queue(reviewed(repos))
    queued_for("t1", deploy.IDLE_WAIT_SEC)
    assert deploy.next_deploy(FakeHost(repos.live, held=True, busy=[1])) is None and systemd_run.argvs == []


def test_a_failed_count_postpones_nothing(repos, systemd_run):
    deploy.queue(reviewed(repos))
    queued_for("t1", deploy.IDLE_WAIT_SEC)
    host = FakeHost(repos.live, busy=[RuntimeError("could not connect to server")])
    assert deploy.next_deploy(host) is None and systemd_run.argvs == []


def test_a_postponed_unit_waits_while_the_reload_lock_is_held(repos, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    lock = repos.tmp / "code-reload.lock"
    monkeypatch.setattr(deploy, "CODE_RELOAD_LOCK", lock)
    deploy.queue(main_spec("c" * 40))
    path = deploy.spec_file(deploy.MAIN)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert unit.main(str(path), postpone=True) == 0
    assert json.loads(deploy.state_file(deploy.MAIN).read_text())["phase"] == "queued"
    assert not path.with_name("result.json").exists() and seen.added == [] and seen.notes == []

    noted = []

    async def notify(message, *, incident_id):
        # The note is the last thing the postponed unit does; it still holds the lock.
        with open(lock, "w") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                noted.append("free")
            except BlockingIOError:
                noted.append("held")
        return True

    monkeypatch.setattr(unit, "notify_ops_slack", notify)
    assert unit.main(str(path), postpone=True) == 1
    assert json.loads(path.with_name("result.json").read_text())["status"] == "postponed" and noted == ["held"]
    assert not deploy.state_file(deploy.MAIN).exists()


def test_a_unit_that_cannot_take_the_lock_leaves_its_run_queued(repos, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    monkeypatch.setattr(deploy, "LOCK_WAIT_SEC", 1)
    host = FakeHost(repos.live)
    monkeypatch.setattr(deploy, "Host", lambda: host)
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    deploy.queue(main_spec(head))
    queued = json.loads(deploy.state_file(deploy.MAIN).read_text())
    with open(deploy.CODE_RELOAD_LOCK, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert unit.main(str(deploy.spec_file(deploy.MAIN))) == 0
    assert json.loads(deploy.state_file(deploy.MAIN).read_text())["phase"] == queued["phase"] == "queued"
    assert not deploy.spec_file(deploy.MAIN).with_name("result.json").exists()
    assert seen.added == [] and seen.notes == [] and seen.client.start_workflow.await_count == 0
    assert not [c for c in host.calls if c[0] in ("systemctl", "count")]
    assert git(repos.live, "rev-parse", "main") == repos.old


def test_a_unit_that_finds_aura_busy_leaves_its_run_queued(repos, monkeypatch):
    head = repos.change({"app/a.py": "A = 2\n"})
    for count in (1, RuntimeError("could not connect to server")):
        host = FakeHost(repos.live, busy=[count])
        assert deploy.self_deploy(spec(repos, head), host, log=lambda m: None) is None
        assert not [c for c in host.calls if c[0] == "systemctl"] and git(repos.live, "rev-parse", "main") == repos.old

    unit, seen = deploy_unit(monkeypatch)
    monkeypatch.setattr(deploy, "Host", lambda: FakeHost(repos.live, busy=[1]))
    deploy.queue(reviewed(repos, head=head))
    assert unit.main(str(deploy.spec_file("t1"))) == 0
    # The pull request landed before the final check, and the run keeps that.
    assert json.loads(deploy.state_file("t1").read_text())["phase"] == "landed"
    assert not deploy.spec_file("t1").with_name("result.json").exists() and seen.added == []
    assert git(repos.live, "rev-parse", "main") == repos.old


def test_a_held_launch_lock_launches_nothing(repos, systemd_run):
    deploy.queue(reviewed(repos))
    with open(deploy.RUNS_DIR / ".launch.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert deploy.next_deploy(FakeHost(repos.live)) is None
    assert systemd_run.argvs == []
    assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-t1" and len(systemd_run.argvs) == 1


def test_a_host_without_a_runs_directory_launches_nothing_and_creates_nothing(repos, github_ci, systemd_run):
    assert not deploy.RUNS_DIR.exists()
    assert deploy.watch_main(FakeHost(repos.live)) is None
    assert systemd_run.argvs == [] and not deploy.RUNS_DIR.exists()


def test_a_run_keeps_one_log_of_its_launches(repos, systemd_run, monkeypatch, capsys):
    log = deploy.spec_file("t1").with_name("deploy.log")
    deploy.queue(reviewed(repos))
    host = FakeHost(repos.live)
    assert deploy.next_deploy(host) == "aura-deploy-t1"
    unit, seen = deploy_unit(monkeypatch)
    # The launch waits: its run stays queued.
    monkeypatch.setattr(deploy, "self_deploy", lambda the_spec, host, *, log: None)
    monkeypatch.setattr(deploy, "Host", lambda: FakeHost(repos.live))
    capsys.readouterr()
    assert unit.main(str(deploy.spec_file("t1"))) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ launch 1 of the deploy of cccccccccccc \(phase queued\)", first)

    assert deploy.next_deploy(host) == "aura-deploy-t1"
    deploy.state_file("t1").unlink()
    deploy.queue(reviewed(repos, head="d" * 40))
    assert deploy.next_deploy(host) == "aura-deploy-t1"
    # A new head that replaces a run still queued, launched once already, is a new run too.
    deploy.queue(reviewed(repos, head="e" * 40))
    assert deploy.next_deploy(host) == "aura-deploy-t1"
    outputs = [[arg for arg in argv if arg.startswith("--property=StandardOutput=")] for argv in systemd_run.argvs]
    assert outputs == [[f"--property=StandardOutput=truncate:{log}"], [f"--property=StandardOutput=append:{log}"],
                       [f"--property=StandardOutput=truncate:{log}"], [f"--property=StandardOutput=truncate:{log}"]]


def test_a_queued_githubs_main_already_shipped_by_a_reviewed_deploy_is_unchanged(repos, tmp_path, monkeypatch):
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    git(repos.live, "merge", "-q", "--ff-only", head)
    write(repos.live, {"app/a.py": "A = 3\n"})
    shipped = commit(repos.live, "a reviewed change deployed after GitHub's main was queued")
    git(repos.live, "push", "-q", "origin", "main")
    host = FakeHost(repos.live)
    result = deploy.self_deploy(main_spec(head), host, log=lambda m: None)
    assert result["status"] == "unchanged" and result["old"] == shipped and result["restarted"] == []
    assert result["summary"] == f"GitHub's main ({head[:12]}) is already live, so nothing changed."
    assert git(repos.live, "rev-parse", "main") == shipped and not [c for c in host.calls if c[0] == "systemctl"]
    assert run_github_unit(tmp_path, monkeypatch, "unchanged", summary=result["summary"]) == (1, [])


def test_a_reviewed_deploy_waits_for_ci_on_the_merge_commit(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    deploy.queue(reviewed(repos, head=head))
    host = FakeHost(repos.live, ci={head: "failure"})
    result = deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
    assert result["status"] == "blocked" and result["pr"]["url"].startswith("https://github.com/")
    assert result["summary"] == (f"CI's test check on the merged commit {head[:12]} is failure, so nothing was deployed "
                                 "here; main is unchanged. GitHub's main has the change; it goes live with GitHub's "
                                 "main once CI passes on a later commit.")
    assert ("wait_check", head) in host.calls and not [c for c in host.calls if c[0] == "systemctl"]
    assert git(repos.live, "rev-parse", "main") == repos.old and git(repos.origin, "rev-parse", "main") == head
    run = json.loads(deploy.state_file("t1").read_text())
    assert run["phase"] == "landed" and run["merge"] == head and run["pr"] == result["pr"]

    class GitHubSquashesAndMovesOn(FakeHost):
        """GitHub squashes the pull request into a commit of its own, and another pull request merges right after."""

        def land(self, commit_sha, branch, title, body, method):
            self.calls.append(("land", branch, method))
            github = repos.tmp / "github"
            git(repos.tmp, "clone", "-q", "-b", "main", str(repos.origin), str(github))
            git(github, "fetch", "-q", str(self.live), "refs/aura/t1")
            git(github, "merge", "-q", "--squash", "FETCH_HEAD")
            merge = commit(github, f"{title} (#7)")
            write(github, {"docs/d.md": "another pull request\n"})
            commit(github, "another pull request")
            git(github, "push", "-q", "origin", "main")
            return {"status": "merged", "pr": {"number": 7, "url": "https://github.com/Hyper-AI-Lab/openclaw-jev/pull/7"},
                    "merge": merge}

    # GitHub's main back at the live main, so the pull request merges anew.
    git(repos.live, "push", "-q", "--force", "origin", f"{repos.old}:refs/heads/main")
    deploy.state_file("t1").unlink()
    host = GitHubSquashesAndMovesOn(repos.live)
    result = deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
    merge, later = git(repos.origin, "rev-parse", "main~1"), git(repos.origin, "rev-parse", "main")
    assert result["status"] == "deployed" and result["head"] == merge and merge not in (head, later)
    assert git(repos.live, "rev-parse", "main") == merge and ("wait_check", merge) in host.calls
    assert git(repos.live, "rev-parse", "main^{tree}") == git(repos.live, "rev-parse", f"{head}^{{tree}}")
    assert git(repos.live, "rev-parse", "refs/remotes/origin/main") == later


def test_a_relaunched_run_that_landed_deploys_its_merge_commit_without_landing_again(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    git(repos.live, "push", "-q", "origin", f"{head}:refs/heads/main")
    deploy.queue(reviewed(repos, head=head))
    pr = {"number": 7, "url": "https://github.com/Hyper-AI-Lab/openclaw-jev/pull/7"}
    run = json.loads(deploy.state_file("t1").read_text())
    deploy.write_json(deploy.state_file("t1"), {**run, "launches": 1, "phase": "landed", "pr": pr, "merge": head})
    host = FakeHost(repos.live)
    result = deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
    assert result["status"] == "deployed" and result["pr"] == pr and git(repos.live, "rev-parse", "main") == head
    assert not [c for c in host.calls if c[0] == "land"] and ("wait_check", head) in host.calls


def test_what_the_previous_deploy_code_left_launches_nothing_and_the_next_head_queues_over_it(repos, github_ci,
                                                                                              systemd_run):
    """This code ships through the deploy code before it, whose unit writes deploy.json, deploy.log and result.json and
    no run state. While that unit runs nothing is queued or launched, its run directories are never launched, and the
    next head of GitHub's main is queued over its spec with the log started anew."""
    shipped = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    git(repos.live, "merge", "-q", "--ff-only", shipped)
    left = {deploy.MAIN: shipped, "t0": "b" * 40}
    for task, head in left.items():
        write(deploy.RUNS_DIR / task, {"deploy.json": json.dumps({"task_id": task, "head": head}) + "\n",
                                       "deploy.log": "the previous code's unit\n"})
    head = repos.change({"app/a.py": "A = 3\n"}, on=shipped)
    git(repos.live, "push", "-q", "origin", f"{head}:refs/heads/main")
    host = FakeHost(repos.live, units=["aura-deploy-main.service"])
    assert deploy.watch_main(host) is None and systemd_run.argvs == [] and deploy.queued_runs() == []

    for task, done in left.items():
        (deploy.RUNS_DIR / task / "result.json").write_text(json.dumps({"status": "deployed", "head": done}) + "\n")
    host.units = []
    assert deploy.watch_main(host) == "aura-deploy-main" and len(systemd_run.argvs) == 1
    assert [task for task, _ in deploy.queued_runs()] == [deploy.MAIN]
    assert json.loads(deploy.spec_file(deploy.MAIN).read_text())["head"] == head
    assert f"--property=StandardOutput=truncate:{deploy.RUNS_DIR / deploy.MAIN / 'deploy.log'}" in systemd_run.argvs[0]


def test_a_dm_during_the_ci_wait_defers_the_restart(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    deploy.queue(reviewed(repos, head=head))
    host = FakeHost(repos.live, busy=[1])
    assert deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None) is None
    assert not [c for c in host.calls if c[0] == "systemctl"] and git(repos.live, "rev-parse", "main") == repos.old
    assert json.loads(deploy.state_file("t1").read_text())["phase"] == "landed"
    assert [c for c in host.calls if c[0] in ("land", "wait_check", "count")] == [
        ("land", "aura/t1-x", "squash"), ("wait_check", head), ("count", 1)]

    result = deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
    assert result["status"] == "deployed" and git(repos.live, "rev-parse", "main") == head
    assert [c for c in host.calls if c[0] == "land"] == [("land", "aura/t1-x", "squash")]


def test_a_task_created_while_the_api_drains_restarts_the_api_and_waits(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    deploy.queue(reviewed(repos, head=head))
    host = FakeHost(repos.live, busy=[0, 1])
    assert deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None) is None
    assert [c for c in host.calls if c[0] in ("systemctl", "count")] == [
        ("count", 0), ("systemctl", "stop", "rmp-api"), ("count", 1), ("systemctl", "start", "rmp-api")]
    assert git(repos.live, "rev-parse", "main") == repos.old
    # The run is back in its phase before applying, with nothing stopped, and is launched again as it was.
    assert state()["phase"] == "landed" and state()["attempt"] == 0 and not deploy.stopped_file("t1").exists()


class MainWatchingHost(FakeHost):
    """Records, with each systemctl call, the live main it saw."""

    def systemctl(self, *args):
        self.calls.append(("systemctl", *args, git(self.live, "rev-parse", "main")))


def test_the_units_stop_before_main_moves_and_start_after_the_switch(repos):
    head = repos.change({"app/a.py": "A = 2\n", "plugins/p/index.js": "v2\n"})
    host = MainWatchingHost(repos.live)
    assert deploy.self_deploy(spec(repos, head), host, log=lambda m: None)["status"] == "deployed"
    old = repos.old
    assert [c for c in host.calls if c[0] in ("systemctl", "count")] == [
        ("count", 0), ("systemctl", "stop", "rmp-api", old), ("count", 0),
        ("systemctl", "stop", "openclaw-gateway", "rmp-worker", old),
        ("systemctl", "start", "openclaw-gateway", "rmp-api", "rmp-worker", head)]


class FailingHost(FakeHost):
    """Raises at the systemctl call ``fail_at`` (its arguments), and at the first ``pip`` when ``pip_fails``; once."""

    def __init__(self, live, *, fail_at=None, pip_fails=False, **kwargs):
        super().__init__(live, **kwargs)
        self.fail_at, self.pip_fails = fail_at, pip_fails

    def systemctl(self, *args):
        super().systemctl(*args)
        if args == self.fail_at:
            self.fail_at = None
            raise subprocess.CalledProcessError(1, ["systemctl", *args])

    def pip_install(self):
        super().pip_install()
        if self.pip_fails:
            self.pip_fails = False
            raise subprocess.CalledProcessError(1, ["pip"])


def test_a_failure_inside_the_window_still_starts_the_units(repos):
    head = repos.change({"app/a.py": "A = 2\n", "requirements.txt": "x==2\n"})
    host = FailingHost(repos.live, fail_at=("stop", "rmp-worker"))
    with pytest.raises(subprocess.CalledProcessError):
        deploy.self_deploy(spec(repos, head), host, log=lambda m: None)
    assert [c for c in host.calls if c[0] == "systemctl"] == [
        ("systemctl", "stop", "rmp-api"), ("systemctl", "stop", "rmp-worker"),
        ("systemctl", "start", "rmp-api", "rmp-worker")]
    assert git(repos.live, "rev-parse", "main") == repos.old

    # Once main moved, a failure is a rollback, and both windows start the units.
    host = FailingHost(repos.live, pip_fails=True)
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)
    assert result["status"] == "rolled_back" and "applying the change failed" in result["summary"]
    assert [c for c in host.calls if c[0] in ("systemctl", "pip")] == [
        ("systemctl", "stop", "rmp-api"), ("systemctl", "stop", "rmp-worker"), ("pip",),
        ("systemctl", "start", "rmp-api", "rmp-worker"),
        ("systemctl", "stop", "rmp-api"), ("systemctl", "stop", "rmp-worker"), ("pip",),
        ("systemctl", "start", "rmp-api", "rmp-worker")]


def test_a_change_that_restarts_nothing_moves_main_without_stopping_anything(repos):
    head = repos.change({"docs/d.md": "more docs\n", "ops/tool.sh": "#!/bin/sh\n", "tests/t.py": "T = 1\n"})
    deploy.queue(reviewed(repos, head=head))
    host = FakeHost(repos.live)
    result = deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
    assert result["status"] == "deployed" and result["restarted"] == [] and "Restarted nothing" in result["summary"]
    assert git(repos.live, "rev-parse", "main") == head
    assert [c for c in host.calls if c[0] in ("systemctl", "count")] == [("count", 0)]
    # Its phases are written all the same, and no list of stopped units is.
    assert state()["phase"] == "restarted" and state()["affected"] == [] and not deploy.stopped_file("t1").exists()


def state(task="t1"):
    return json.loads(deploy.state_file(task).read_text())


@pytest.mark.parametrize("source", ["reviewed", "github"])
def test_a_unit_killed_after_the_fast_forward_resumes_from_its_merged_mark(repos, systemd_run, monkeypatch, source):
    unit, seen = deploy_unit(monkeypatch)
    files = {"app/a.py": "A = 2\n", "plugins/p/index.js": "v2\n"}
    if source == "github":
        head = merged_on_github(repos, files)
        task, the_spec = deploy.MAIN, main_spec(head)
    else:
        head = repos.change(files)
        task, the_spec = "t1", reviewed(repos, head=head)
    deploy.queue(the_spec)
    path = deploy.spec_file(task)
    killed, resumed = FakeHost(repos.live, kill_at=("start", 1)), FakeHost(repos.live)
    hosts = [killed, resumed]
    monkeypatch.setattr(deploy, "Host", lambda: hosts.pop(0))
    units = ("openclaw-gateway", "rmp-api", "rmp-worker")

    assert deploy.next_deploy(FakeHost(repos.live)) == f"aura-deploy-{task}"
    assert unit.main(str(path)) == 1
    run = state(task)
    assert run["phase"] == "merged" and run["old"] == repos.old and run["head"] == head and run["escapes"] == 1
    assert git(repos.live, "rev-parse", "main") == head and not path.with_name("result.json").exists()
    # The window starts what it stopped on its way out.
    assert [c for c in killed.calls if c[0] == "systemctl"][-1] == ("systemctl", "start", *units)
    assert seen.added == [] and seen.notes == [] and seen.client.start_workflow.await_count == 0

    assert deploy.next_deploy(FakeHost(repos.live)) == f"aura-deploy-{task}"
    assert "--postpone" not in systemd_run.argvs[-1]
    assert unit.main(str(path)) == 0
    result = json.loads(path.with_name("result.json").read_text())
    assert result["status"] == "deployed" and result["old"] == repos.old and result["restarted"] == list(units)
    # No step before the change runs again: no land, no CI wait, no count, no hand-edit or ancestry check.
    assert [c for c in resumed.calls if c[0] in ("systemctl", "checks", "land", "wait_check", "count")] == [
        *switched(*units), ("checks", units, ("telemetry",))]
    assert [event.event_payload["status"] for event in seen.added] == ["deployed"] and seen.notes == []
    if source == "reviewed":
        assert seen.client.start_workflow.await_args.args[1]["report"]["shipped"]["status"] == "deployed"
    assert (repos.mirror["plugins/"] / "p" / "index.js").read_text() == "v2\n"
    assert not deploy.state_file(task).exists()


def test_a_resumed_run_is_never_postponed(repos, systemd_run, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    deploy.queue(main_spec(head))
    queued_for(deploy.MAIN, 3 * 3600)
    monkeypatch.setattr(deploy, "Host", lambda: FakeHost(repos.live, kill_at=("start", 1)))
    assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-main"
    assert unit.main(str(deploy.spec_file(deploy.MAIN))) == 1 and state(deploy.MAIN)["phase"] == "merged"
    launches = len(systemd_run.argvs)

    assert deploy.next_deploy(FakeHost(repos.live, busy=[1])) is None and len(systemd_run.argvs) == launches
    assert state(deploy.MAIN)["phase"] == "merged"
    assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-main" and len(systemd_run.argvs) == launches + 1
    assert systemd_run.argvs[-1][-1] == str(deploy.spec_file(deploy.MAIN))


def test_a_unit_killed_while_reverting_publishes_the_revert_once(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    failed = {"failure": "CANARY FAIL: task failed", "canary": None}
    # Killed in the revert window before its tree moved, and after.
    for kill_at, main_at in ((("stop", 3), "head"), (("start", 2), "revert")):
        git(repos.live, "reset", "-q", "--hard", repos.old)
        git(repos.live, "push", "-q", "--force", "origin", f"{repos.old}:refs/heads/main")
        for path in (deploy.spec_file("t1"), deploy.state_file("t1")):
            path.unlink(missing_ok=True)
        deploy.queue(reviewed(repos, head=head))
        host = FakeHost(repos.live, checks=[failed], kill_at=kill_at)
        with pytest.raises(deploy.Interrupted):
            deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
        run = state()
        assert run["phase"] == "reverting" and run["failure"] == failed["failure"] and run["revert"]
        assert git(repos.live, "rev-parse", "main") == run[main_at]
        assert not [c for c in host.calls if c[1:] == (f"aura/revert-{head[:12]}", "merge")]

        host = FakeHost(repos.live)
        result = deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
        assert result["status"] == "rolled_back" and result["revert"] == run["revert"]
        assert git(repos.live, "rev-parse", "main") == run["revert"] == git(repos.origin, "rev-parse", "main")
        assert git(repos.live, "rev-parse", f"{run['revert']}^{{tree}}") == git(repos.live, "rev-parse",
                                                                                f"{repos.old}^{{tree}}")
        assert [c for c in host.calls if c[0] == "land"] == [("land", f"aura/revert-{head[:12]}", "merge")]
        assert [c for c in host.calls if c[0] in ("systemctl", "checks")] == [
            *switched("rmp-api", "rmp-worker"), ("checks", ("rmp-api", "rmp-worker"), ("telemetry",))]


def test_a_run_to_resume_launches_before_the_queued_runs(repos, systemd_run):
    deploy.queue(reviewed(repos, "t1"))
    deploy.queue(main_spec("c" * 40))
    deploy.queue(reviewed(repos, "t2"))
    deploy.write_json(deploy.state_file("t2"), {**state("t2"), "phase": "merged"})
    deploy.write_json(deploy.state_file(deploy.MAIN), {**state(deploy.MAIN), "phase": "restarted"})
    assert [task for task, _ in deploy.queued_runs()] == ["t2", deploy.MAIN, "t1"]
    assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-t2"


def test_the_fast_forward_waits_for_a_full_reserve(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    git(repos.live, "push", "-q", "origin", f"{head}:refs/heads/main")
    deploy.queue(main_spec(head))
    host = FakeHost(repos.live)
    host.elapsed = lambda: deploy.DEPLOY_UNIT_MAX_SEC - deploy.APPLY_RESERVE_SEC + 1
    assert deploy.self_deploy(main_spec(head), host, log=lambda m: None) is None
    assert state(deploy.MAIN)["phase"] == "queued" and state(deploy.MAIN)["attempt"] == 0
    assert git(repos.live, "rev-parse", "main") == repos.old
    assert not [c for c in host.calls if c[0] in ("systemctl", "count")]

    host.elapsed = lambda: deploy.DEPLOY_UNIT_MAX_SEC - deploy.APPLY_RESERVE_SEC
    assert deploy.self_deploy(main_spec(head), host, log=lambda m: None)["status"] == "deployed"


def test_a_resume_that_finds_a_foreign_main_fails_and_starts_the_units(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    deploy.queue(reviewed(repos, head=head))
    deploy.write_json(deploy.state_file("t1"), {**state(), "phase": "merged", "old": repos.old, "head": head,
                                                "affected": ["rmp-api", "rmp-worker"], "baseline": []})
    write(repos.live, {"docs/d.md": "someone else's commit\n"})
    foreign = commit(repos.live, "a commit no deploy made")
    host = FakeHost(repos.live)
    result = deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
    assert result == {"status": "failed",
                      "summary": f"The live main is {foreign[:12]}, which this deploy did not make; please check it."}
    assert [c for c in host.calls if c[0] in ("systemctl", "land", "checks")] == [
        ("systemctl", "start", "rmp-api", "rmp-worker")]
    assert git(repos.live, "rev-parse", "main") == foreign


@pytest.mark.parametrize("phase, at", [("applying", "old"), ("applying", "head"), ("merged", "head"),
                                       ("restarted", "head")])
def test_each_phase_of_a_killed_run_resumes_where_it_stopped(repos, phase, at):
    head = repos.change({"app/a.py": "A = 2\n"})
    deploy.queue(reviewed(repos, head=head))
    git(repos.live, "push", "-q", "origin", f"{head}:refs/heads/main")
    pr = {"number": 7, "url": "https://github.com/Hyper-AI-Lab/openclaw-jev/pull/7"}
    deploy.write_json(deploy.state_file("t1"), {**state(), "phase": phase, "pr": pr, "merge": head, "old": repos.old,
                                                "head": head, "affected": ["rmp-api", "rmp-worker"], "baseline": []})
    if at == "head":
        git(repos.live, "merge", "-q", "--ff-only", head)
    host = FakeHost(repos.live)
    result = deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
    assert result["status"] == "deployed" and result["pr"] == pr and git(repos.live, "rev-parse", "main") == head
    assert state()["phase"] == "restarted"
    stop_api, stop_worker, start = switched("rmp-api", "rmp-worker")
    calls = [c for c in host.calls if c[0] in ("systemctl", "count", "land", "wait_check")]
    if at == "old":
        # Its units start first (they may be stopped), then the run goes through the final count and the window.
        assert calls == [start, ("count", 0), stop_api, ("count", 0), stop_worker, start]
    else:
        # A run whose tree moved finishes its window without counting, or only runs its checks once restarted.
        assert calls == ([] if phase == "restarted" else [stop_api, stop_worker, start])


def test_sigterm_in_the_window_starts_the_units_and_keeps_the_run(repos, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    host = FakeHost(repos.live)
    monkeypatch.setattr(deploy, "Host", lambda: host)
    real_reconcile = deploy.reconcile

    def reconcile(*args):
        # The first step after the fast-forward; systemd's stop sends SIGTERM.
        signal.raise_signal(signal.SIGTERM)
        return real_reconcile(*args)

    monkeypatch.setattr(deploy, "reconcile", reconcile)
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    deploy.queue(main_spec(head))
    before = signal.getsignal(signal.SIGTERM)

    assert unit.main(str(deploy.spec_file(deploy.MAIN))) == 1
    assert signal.getsignal(signal.SIGTERM) == before
    assert state(deploy.MAIN)["phase"] == "merged" and git(repos.live, "rev-parse", "main") == head
    assert state(deploy.MAIN)["escapes"] == 1 and state(deploy.MAIN)["attempt"] == 0
    assert [c for c in host.calls if c[0] == "systemctl"] == switched("rmp-api", "rmp-worker")
    assert not deploy.spec_file(deploy.MAIN).with_name("result.json").exists() and seen.added == [] and seen.notes == []
    assert not deploy.stopped_file(deploy.MAIN).exists()


def test_the_window_lists_its_stopped_units_until_they_start(repos, systemd_run):
    class ListWatchingHost(FakeHost):
        """Records, with each systemctl call, the list of stopped units as it was."""

        def systemctl(self, *args):
            listed = deploy.stopped_file("t1")
            self.calls.append(("systemctl", *args, listed.read_text() if listed.exists() else None))

    head = repos.change({"app/a.py": "A = 2\n", "plugins/p/index.js": "v2\n"})
    deploy.queue(reviewed(repos, head=head))
    host = ListWatchingHost(repos.live)
    assert deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)["status"] == "deployed"
    listed = "openclaw-gateway\nrmp-api\nrmp-worker\n"
    window = [("systemctl", "stop", "rmp-api", listed), ("systemctl", "stop", "openclaw-gateway", "rmp-worker", listed),
              ("systemctl", "start", "openclaw-gateway", "rmp-api", "rmp-worker", listed)]
    assert [c for c in host.calls if c[0] == "systemctl"] == window
    assert not deploy.stopped_file("t1").exists()

    # The revert window lists the same units the same way.
    repos.old = head
    head = repos.change({"app/a.py": "A = 3\n", "plugins/p/index.js": "v3\n"}, on=head)
    for path in (deploy.spec_file("t1"), deploy.state_file("t1")):
        path.unlink()
    deploy.queue(reviewed(repos, head=head))
    host = ListWatchingHost(repos.live, checks=[{"failure": "CANARY FAIL: task failed", "canary": None}])
    assert deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)["status"] == "rolled_back"
    assert [c for c in host.calls if c[0] == "systemctl"] == window * 2
    assert not deploy.stopped_file("t1").exists()

    deploy.state_file("t1").unlink()
    deploy.queue(reviewed(repos, "t2"))
    assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-t2"
    listed = deploy.RUNS_DIR / "t2" / "stopped-units"
    assert (f"--property=ExecStopPost=/bin/sh -c 'test -s {listed} && xargs -r systemctl start --no-block < {listed}; "
            "exit 0'") in systemd_run.argvs[-1]


def test_the_stop_post_command_starts_the_listed_units(tmp_path):
    fakes, calls = tmp_path / "bin", tmp_path / "systemctl.log"
    fakes.mkdir()
    fake = fakes / "systemctl"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}"}
    # xargs runs only in front of the fake systemctl.
    assert shutil.which("systemctl", path=env["PATH"]) == str(fake)
    assert shutil.which("xargs", path=env["PATH"]) and not shutil.which("xargs", path=env["PATH"]).startswith(str(fakes))
    run_dir = tmp_path / "t1"
    run_dir.mkdir()
    command = deploy.stop_post_command(run_dir)

    (run_dir / "stopped-units").write_text("rmp-api\nrmp-worker\n")
    assert subprocess.run(["/bin/sh", "-c", command], env=env, timeout=30).returncode == 0
    assert calls.read_text() == "start --no-block rmp-api rmp-worker\n"

    calls.unlink()
    (run_dir / "stopped-units").unlink()
    assert subprocess.run(["/bin/sh", "-c", command], env=env, timeout=30).returncode == 0
    assert not calls.exists()


class BlippingHost(FakeHost):
    """The readiness baseline (a Postgres blip behind the API) and CI's check on the merged commit (GitHub) raise their
    queued errors in turn, then answer."""

    def __init__(self, live, *, baselines=(), waits=(), **kwargs):
        super().__init__(live, **kwargs)
        self.baselines, self.waits = list(baselines), list(waits)

    def readiness_report(self):
        if self.baselines:
            raise self.baselines.pop(0)
        return super().readiness_report()

    def wait_check(self, sha):
        if self.waits:
            self.calls.append(("wait_check", sha))
            raise self.waits.pop(0)
        return super().wait_check(sha)


def test_a_transient_error_before_the_change_is_retried(repos, monkeypatch, systemd_run):
    unit, seen = deploy_unit(monkeypatch)
    host = BlippingHost(repos.live, baselines=[RuntimeError("could not connect to server")])
    monkeypatch.setattr(deploy, "Host", lambda: host)
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    deploy.queue(main_spec(head))
    path = deploy.spec_file(deploy.MAIN)

    assert unit.main(str(path)) == 1
    run = state(deploy.MAIN)
    assert run["phase"] == "queued" and run["attempt"] == 1 and run["last_error"] == "could not connect to server"
    assert not path.with_name("result.json").exists() and seen.added == [] and seen.notes == []
    assert git(repos.live, "rev-parse", "main") == repos.old and not [c for c in host.calls if c[0] == "systemctl"]

    assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-main"
    assert unit.main(str(path)) == 0
    assert json.loads(path.with_name("result.json").read_text())["status"] == "deployed" and seen.notes == []
    assert git(repos.live, "rev-parse", "main") == head and not deploy.state_file(deploy.MAIN).exists()


def test_three_errors_end_the_run_failed(repos, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    host = BlippingHost(repos.live, baselines=[RuntimeError(f"blip {n}") for n in range(1, 4)])
    monkeypatch.setattr(deploy, "Host", lambda: host)
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    deploy.queue(main_spec(head))
    path = deploy.spec_file(deploy.MAIN)

    assert [unit.main(str(path)) for _ in range(3)] == [1, 1, 1]
    result = json.loads(path.with_name("result.json").read_text())
    assert result["status"] == "failed" and result["summary"] == (
        "The deploy failed 3 times before changing anything (last: blip 3); main is unchanged.")
    [(note, incident)] = seen.notes
    assert note.startswith("Aura's deploy: failed.") and incident == f"deploy:main:{head[:12]}"
    assert not deploy.state_file(deploy.MAIN).exists() and git(repos.live, "rev-parse", "main") == repos.old


def test_a_github_error_while_waiting_for_ci_on_the_merge_commit_is_retried(repos, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    head = repos.change({"app/a.py": "A = 2\n"})
    error = deploy.github.GitHubError("GET repos/x/commits/y/check-runs: 502 Bad Gateway")
    host = BlippingHost(repos.live, waits=[error])
    monkeypatch.setattr(deploy, "Host", lambda: host)
    deploy.queue(reviewed(repos, head=head))
    path = deploy.spec_file("t1")

    assert unit.main(str(path)) == 1
    assert state()["phase"] == "landed" and state()["attempt"] == 1 and seen.added == []
    assert unit.main(str(path)) == 0
    shipped = seen.client.start_workflow.await_args.args[1]["report"]["shipped"]
    assert shipped["status"] == "deployed" and git(repos.live, "rev-parse", "main") == head
    assert [c for c in host.calls if c[0] == "land"] == [("land", "aura/t1-x", "squash")]

    # Three errors: the run ends, and the change waits on GitHub's main.
    head = repos.change({"app/a.py": "A = 3\n"}, on=head)
    repos.old = git(repos.live, "rev-parse", "main")
    host = BlippingHost(repos.live, waits=[error] * 3)
    path.with_name("result.json").unlink()
    deploy.queue(reviewed(repos, head=head))
    assert [unit.main(str(path)) for _ in range(3)] == [1, 1, 1]
    result = json.loads(path.with_name("result.json").read_text())
    assert result["status"] == "failed" and result["summary"] == (
        f"The deploy failed 3 times before changing anything (last: {error}); main is unchanged. GitHub's main has the "
        "change; it goes live with GitHub's main.")
    assert [c for c in host.calls if c[0] == "land"] == [("land", "aura/t1-x", "squash")]
    assert git(repos.live, "rev-parse", "main") == repos.old and not deploy.state_file("t1").exists()


@pytest.fixture
def reporting(repos, monkeypatch):
    """The reconciler's report of ended deploy runs, with the database (``db``), Slack and Temporal faked: the events it
    added (``events``), the notes it sent (``notes``) and the reply runs it started (``client``); ``host`` is the
    deploy host it sees."""
    from app import config, reconciler
    from app.production import ops_notify

    seen = SimpleNamespace(events=[], notes=[], client=SimpleNamespace(start_workflow=AsyncMock()),
                           host=FakeHost(repos.live))
    seen.db = SimpleNamespace(add=seen.events.append, get=AsyncMock(return_value=None), commit=AsyncMock())
    session = AsyncMock()
    session.__aenter__.return_value = seen.db
    monkeypatch.setattr(reconciler, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(reconciler, "connect_temporal", AsyncMock(return_value=seen.client))
    monkeypatch.setattr(config, "is_development_mode", lambda: False)
    monkeypatch.setattr(config, "get_coding_config",
                        lambda: {"repositories": {"rmp": {"remote": "Hyper-AI-Lab/openclaw-jev"}}})

    async def notify(message, *, incident_id):
        seen.notes.append((message, incident_id))
        return True

    monkeypatch.setattr(ops_notify, "notify_ops_slack", notify)
    monkeypatch.setattr(deploy, "Host", lambda *args, **kwargs: seen.host)
    seen.report = reconciler.report_ended_deploys
    return seen


def launched(task, launches, started=None, **fields):
    """Give the run of ``task`` ``launches`` launches, of which ``started`` checked in (all when None)."""
    deploy.write_json(deploy.state_file(task), {**state(task), "launches": launches,
                                                "started": launches if started is None else started, **fields})


async def test_a_run_whose_units_die_before_they_start_ends_failed_once(repos, systemd_run, reporting):
    deploy.queue(main_spec("c" * 40))
    host = FakeHost(repos.live)
    # Each launch's unit dies before it runs main: it never checks in.
    assert [deploy.next_deploy(host) for _ in range(4)] == ["aura-deploy-main"] * 3 + [None]
    assert len(systemd_run.argvs) == 3 and state(deploy.MAIN)["launches"] == 3 and state(deploy.MAIN)["started"] == 0

    await reporting.report()
    log = deploy.RUNS_DIR / "main" / "deploy.log"
    result = {"status": "failed", "source": "github", "head": "c" * 40, "ci": "success",
              "summary": f"The deploy unit ended 3 times before it started (see {log}), so nothing was deployed; "
                         "main is unchanged."}
    assert json.loads(deploy.spec_file(deploy.MAIN).with_name("result.json").read_text()) == result
    assert not deploy.state_file(deploy.MAIN).exists()
    [event] = reporting.events
    assert (event.event_type, event.entity_type, event.entity_id, event.event_payload) == (
        "coding.deploy", "deploy", "main", {**result, "note_delivered": True})
    [(note, incident)] = reporting.notes
    assert note.startswith("Aura's deploy: failed.") and result["summary"] in note
    assert incident == "deploy:main:cccccccccccc"
    reporting.client.start_workflow.assert_not_awaited()

    await reporting.report()
    assert len(reporting.events) == 1 and len(reporting.notes) == 1


def test_a_run_whose_third_launch_is_starting_is_not_ended(repos):
    deploy.queue(reviewed(repos))
    launched("t1", 3, started=0)
    before = deploy.state_file("t1").read_text()
    assert deploy.end_dead_runs(FakeHost(repos.live, units=["aura-deploy-t1.service"])) == []
    assert deploy.state_file("t1").read_text() == before


def test_a_unit_whose_run_has_no_state_does_nothing(repos, monkeypatch, capsys):
    unit, seen = deploy_unit(monkeypatch)
    monkeypatch.setattr(deploy, "self_deploy", lambda *args, **kwargs: pytest.fail("a run without a state deployed"))
    deploy.queue(reviewed(repos))
    deploy.state_file("t1").unlink()
    for postpone in (False, True):
        assert unit.main(str(deploy.spec_file("t1")), postpone=postpone) == 0
        assert capsys.readouterr().out.splitlines()[-1].endswith(" the run of t1 has no state; nothing to do")
    assert not deploy.spec_file("t1").with_name("result.json").exists() and not deploy.state_file("t1").exists()
    assert seen.added == [] and seen.notes == [] and seen.client.start_workflow.await_count == 0


async def test_a_dead_reviewed_run_replies_and_lets_githubs_main_deploy(repos, systemd_run, reporting):
    deploy.queue(reviewed(repos))
    launched("t1", 3, started=0)
    deploy.queue(main_spec("c" * 40))
    assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-main"

    await reporting.report()
    assert reporting.notes == [] and not deploy.state_file("t1").exists()
    name, payload = reporting.client.start_workflow.await_args.args
    assert name == "CodingTaskWorkflow" and reporting.client.start_workflow.await_args.kwargs["id"] == "workflow-t1"
    assert payload["task_id"] == "t1" and payload["report"]["shipped"]["status"] == "failed"
    assert payload["report"]["shipped"]["source"] == "reviewed"
    [event] = reporting.events
    assert event.entity_type == "task" and event.event_payload == payload["report"]["shipped"]
    assert [argv[1] for argv in systemd_run.argvs] == ["--unit=aura-deploy-main"]


@pytest.mark.parametrize("deaths", [{"started": 0}, {"escapes": 3}])
async def test_a_dead_run_past_applying_keeps_its_state_and_stops_the_queue(repos, systemd_run, reporting, deaths):
    head = repos.change({"app/a.py": "A = 2\n"})
    git(repos.live, "merge", "-q", "--ff-only", head)
    deploy.queue(reviewed(repos, head=head))
    launched("t1", 3, phase="merged", old=repos.old, head=head, affected=["rmp-api", "rmp-worker"], baseline=[],
             previous="/root/.openclaw/venvs/rmp/0123456789abcdef", **deaths)
    deploy.queue(main_spec("c" * 40))
    assert deploy.next_deploy(FakeHost(repos.live)) is None

    await reporting.report()
    kept = state()
    assert kept["phase"] == "merged" and kept["dead_noted"] is True
    assert kept["old"] == repos.old and kept["previous"] == "/root/.openclaw/venvs/rmp/0123456789abcdef"
    [(note, incident)] = reporting.notes
    log = deploy.RUNS_DIR / "t1" / "deploy.log"
    assert note.startswith("Aura's deploy: stuck.") and (
        f"The deploy unit ended 3 times in phase merged with the live main at {head[:12]}, moved from "
        f"{repos.old[:12]} and not verified (see {log}); nothing else deploys until it is resumed by hand "
        "(specs/WP-09.md §0).") in note
    assert incident == f"deploy:t1:{head[:12]}" and reporting.client.start_workflow.await_count == 0
    assert not deploy.spec_file("t1").with_name("result.json").exists()

    await reporting.report()
    assert len(reporting.notes) == 1 and len(reporting.events) == 1
    assert deploy.next_deploy(FakeHost(repos.live)) is None and systemd_run.argvs == []


async def test_a_dead_run_is_reported_while_github_cannot_be_read(repos, systemd_run, reporting, monkeypatch):
    from app import reconciler

    deploy.queue(main_spec("c" * 40))
    launched(deploy.MAIN, 3, started=0)

    def fetch_main():
        raise RuntimeError("git fetch failed: Could not resolve host: github.com")

    reporting.host.fetch_main = fetch_main
    monkeypatch.setattr(reconciler, "prune_coding_jobs", lambda: [])
    stop = asyncio.Event()

    async def reconcile_once():
        stop.set()
        return {"skipped": True}

    monkeypatch.setattr(reconciler, "reconcile_once", reconcile_once)
    await reconciler.reconciler_loop(stop)
    [(note, incident)] = reporting.notes
    assert "The deploy unit ended 3 times before it started" in note and incident == "deploy:main:cccccccccccc"
    assert not deploy.state_file(deploy.MAIN).exists() and systemd_run.argvs == []


def test_a_unit_that_starts_and_waits_is_not_dead(repos, systemd_run, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    monkeypatch.setattr(deploy, "Host", lambda: FakeHost(repos.live, busy=[1]))
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    deploy.queue(main_spec(head))
    for launch in range(1, 5):
        assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-main"
        assert unit.main(str(deploy.spec_file(deploy.MAIN))) == 0
        assert state(deploy.MAIN)["started"] == state(deploy.MAIN)["launches"] == launch
    assert deploy.end_dead_runs(FakeHost(repos.live)) == [] and state(deploy.MAIN)["phase"] == "queued"


def test_a_run_queued_by_batch_1_is_not_dead(repos, systemd_run):
    deploy.queue(main_spec("c" * 40))
    # Batch 1's state: no started, no escapes.
    deploy.write_json(deploy.state_file(deploy.MAIN), {"phase": "queued", "queued_at": state(deploy.MAIN)["queued_at"],
                                                       "attempt": 0, "launches": 4})
    assert deploy.end_dead_runs(FakeHost(repos.live)) == []
    assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-main"
    assert state(deploy.MAIN)["launches"] == 5 and state(deploy.MAIN)["started"] == 4
    # With the launch that gave it started, three launches that never check in make it dead.
    assert [deploy.next_deploy(FakeHost(repos.live)) for _ in range(3)] == ["aura-deploy-main"] * 2 + [None]
    [(task, spec, result)] = deploy.end_dead_runs(FakeHost(repos.live))
    assert task == deploy.MAIN and result["status"] == "failed" and spec == main_spec("c" * 40)


def test_aborting_a_queued_run_by_hand_ends_it_and_main_is_not_queued_again(repos, github_ci, systemd_run):
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    deploy.queue(main_spec(head))
    deploy.write_json(deploy.state_file(deploy.MAIN), {**state(deploy.MAIN), "attempt": 1})
    outcome = []
    # The abort waits out a launch: it takes the launch lock, blocking.
    with open(deploy.RUNS_DIR / ".launch.lock", "w") as launching:
        fcntl.flock(launching, fcntl.LOCK_EX)
        aborting = threading.Thread(target=lambda: outcome.append(deploy.abort_run(deploy.MAIN)))
        aborting.start()
        aborting.join(0.3)
        assert aborting.is_alive() and systemd_run.stopped == []
    aborting.join(10)
    assert outcome == ["Aborted the deploy of main in phase queued: it is recorded failed, and its state is removed."]
    assert systemd_run.stopped == ["aura-deploy-main"] and not deploy.state_file(deploy.MAIN).exists()
    assert json.loads(deploy.spec_file(deploy.MAIN).with_name("result.json").read_text()) == {
        "status": "failed", "source": "github", "head": head,
        "summary": "Aborted by hand; nothing was deployed; main is unchanged."}
    assert deploy.watch_main(FakeHost(repos.live)) is None
    assert not deploy.state_file(deploy.MAIN).exists() and systemd_run.argvs == []

    deploy.queue(reviewed(repos))
    deploy.write_json(deploy.state_file("t1"), {**state(), "phase": "landed"})
    deploy.abort_run("t1")
    assert json.loads(deploy.spec_file("t1").with_name("result.json").read_text())["summary"] == (
        "Aborted by hand; nothing was deployed; main is unchanged. GitHub's main has the change; it goes live with "
        "GitHub's main.")


def test_aborting_a_run_past_applying_keeps_it(repos, systemd_run):
    deploy.queue(reviewed(repos))
    deploy.write_json(deploy.state_file("t1"), {**state(), "phase": "merged", "old": repos.old, "head": "c" * 40})
    before = deploy.state_file("t1").read_text()
    assert deploy.abort_run("t1") == ("The deploy of t1 is in phase merged, past the change, so its state is kept; "
                                      "the next idle pass resumes it.")
    assert systemd_run.stopped == ["aura-deploy-t1"] and deploy.state_file("t1").read_text() == before
    assert not deploy.spec_file("t1").with_name("result.json").exists()


def batch_1_state(launches, **fields):
    """A run's state as the deploy code before this one writes it: no ``started``, no ``escapes``."""
    queued_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    return {"phase": "queued", "queued_at": queued_at, "attempt": 0, "launches": launches, **fields}


def test_this_code_leaves_a_unit_of_the_previous_deploy_code_alone_while_it_runs(repos, github_ci, systemd_run):
    """This code ships through the deploy code before it: its unit deploys GitHub's main while rmp-api, restarted on
    this code, runs the reconciler's pass. That pass ends nothing and launches nothing while the unit runs, and nothing
    once the unit recorded its result and removed the run's state."""
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    deploy.queue(main_spec(head))
    deploy.write_json(deploy.state_file(deploy.MAIN), batch_1_state(1))
    before = deploy.state_file(deploy.MAIN).read_text()
    host = FakeHost(repos.live, units=["aura-deploy-main.service"])
    assert deploy.end_dead_runs(host) == [] and deploy.watch_main(host) is None
    assert deploy.state_file(deploy.MAIN).read_text() == before and systemd_run.argvs == []

    git(repos.live, "merge", "-q", "--ff-only", head)
    deploy.write_json(deploy.spec_file(deploy.MAIN).with_name("result.json"), {"status": "deployed", "head": head})
    deploy.state_file(deploy.MAIN).unlink()
    host.units = []
    assert deploy.end_dead_runs(host) == [] and deploy.watch_main(host) is None and systemd_run.argvs == []


@pytest.mark.parametrize("source", ["reviewed", "github"])
def test_a_run_the_previous_deploy_code_left_is_launched_and_deployed_by_this_code(repos, systemd_run, monkeypatch,
                                                                                  source):
    """A run the previous deploy code queued, or landed and left waiting, is launched by this launcher, which gives
    its state ``started``, and deployed by this unit: a landed run at exactly its recorded merge, without landing
    again."""
    unit, seen = deploy_unit(monkeypatch)
    host = FakeHost(repos.live)
    monkeypatch.setattr(deploy, "Host", lambda: host)
    if source == "github":
        head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
        task, left = deploy.MAIN, batch_1_state(2)
        deploy.queue(main_spec(head))
    else:
        head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
        pr = {"number": 7, "url": "https://github.com/Hyper-AI-Lab/openclaw-jev/pull/7"}
        task, left = "t1", batch_1_state(2, phase="landed", pr=pr, merge=head)
        deploy.queue(reviewed(repos, head=head))
    deploy.write_json(deploy.state_file(task), left)

    assert deploy.next_deploy(FakeHost(repos.live)) == f"aura-deploy-{task}"
    assert {key: state(task)[key] for key in ("phase", "launches", "started")} == {
        "phase": left["phase"], "launches": 3, "started": 2}
    assert unit.main(str(deploy.spec_file(task))) == 0
    result = json.loads(deploy.spec_file(task).with_name("result.json").read_text())
    assert result["status"] == "deployed" and result["head"] == head and git(repos.live, "rev-parse", "main") == head
    assert not [c for c in host.calls if c[0] == "land"] and not deploy.state_file(task).exists()


def test_a_hand_edit_made_during_the_readiness_baseline_blocks_the_deploy(repos):
    rule = repos.mirror[".cursor/rules/"] / "r.mdc"

    class EditedDuringTheBaseline(FakeHost):
        def readiness_report(self):
            rule.write_text("edited by hand while readiness ran\n")
            return super().readiness_report()

    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    host = EditedDuringTheBaseline(repos.live)
    result = deploy.self_deploy(main_spec(head), host, log=lambda m: None)
    assert result["status"] == "blocked" and ".cursor/rules/r.mdc" in result["summary"]
    assert git(repos.live, "rev-parse", "main") == repos.old and rule.read_text() == "edited by hand while readiness ran\n"
    assert not [c for c in host.calls if c[0] in ("systemctl", "count")]


@pytest.mark.parametrize("failed_reads", [1, 2])
def test_a_failure_reading_the_live_main_in_the_window_is_an_error_before_the_change(repos, monkeypatch,
                                                                                      failed_reads):
    head = repos.change({"app/a.py": "A = 2\n"})
    deploy.queue(reviewed(repos, head=head))
    real_git, armed = deploy._git, []

    def git_failing(repo, *args, env=None):
        if armed and args == ("rev-parse", "refs/heads/main"):
            armed.pop()
            raise RuntimeError("git rev-parse failed: unable to read refs")
        return real_git(repo, *args, env=env)

    class ArmedAtTheFinalCount(FakeHost):
        """The next reads of the live main after the final count fail: the window's own, and its read after that."""

        def active_user_tasks(self):
            armed.extend([True] * failed_reads)
            return super().active_user_tasks()

    monkeypatch.setattr(deploy, "_git", git_failing)
    host = ArmedAtTheFinalCount(repos.live)
    with pytest.raises(RuntimeError, match="unable to read refs"):
        deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
    # Read again, the main is still at old: an error before the change. Unreadable twice, the run keeps applying,
    # and its resume reads the live main; either way nothing is reverted.
    assert state()["phase"] == ("landed" if failed_reads == 1 else "applying")
    assert git(repos.live, "rev-parse", "main") == repos.old
    assert not [c for c in host.calls if c[0] in ("checks", "pip") or c[1:] == (f"aura/revert-{head[:12]}", "merge")]
    assert git(repos.origin, "rev-parse", "main") == head


@pytest.mark.parametrize("stop", [deploy.Interrupted("SIGTERM"), RuntimeError("the log could not be written")])
def test_a_stop_right_after_the_fast_forward_counts_as_moved(repos, stop):
    head = repos.change({"app/a.py": "A = 2\n"})
    deploy.queue(reviewed(repos, head=head))
    stops = [stop]

    def log(message):
        # The first line after git merge returned; the stop comes once.
        if message.startswith("main moved") and stops:
            raise stops.pop()

    host = FakeHost(repos.live)
    if isinstance(stop, deploy.Interrupted):
        with pytest.raises(deploy.Interrupted):
            deploy.self_deploy(reviewed(repos, head=head), host, log=log)
        # Killed after git merge returned and before the phase said so: the run resumes from applying at head.
        assert state()["phase"] == "applying" and git(repos.live, "rev-parse", "main") == head
        assert [c for c in host.calls if c[0] == "systemctl"][-1] == ("systemctl", "start", "rmp-api", "rmp-worker")
        resumed = deploy.self_deploy(reviewed(repos, head=head), FakeHost(repos.live), log=lambda m: None)
        assert resumed["status"] == "deployed" and git(repos.live, "rev-parse", "main") == head
    else:
        result = deploy.self_deploy(reviewed(repos, head=head), host, log=log)
        assert result["status"] == "rolled_back" and "the log could not be written" in result["summary"]
        assert git(repos.live, "rev-parse", f"{result['revert']}^{{tree}}") == git(repos.live, "rev-parse",
                                                                                   f"{repos.old}^{{tree}}")


def window_lines(lines):
    """The log lines of the switch windows: each stop, the move and each start."""
    return [line for line in lines if line.startswith(("stopped ", "started ", "main moved "))]


def test_the_deploy_log_shows_stop_move_start_in_both_windows(repos):
    head = repos.change({"app/a.py": "A = 2\n", "plugins/p/index.js": "v2\n"})
    lines = []
    host = FakeHost(repos.live, checks=[{"failure": "CANARY FAIL: task failed", "canary": None}])
    result = deploy.self_deploy(spec(repos, head), host, log=lines.append)
    assert result["status"] == "rolled_back"
    old, revert, units = repos.old[:12], result["revert"][:12], "openclaw-gateway, rmp-api, rmp-worker"
    assert window_lines(lines) == [
        "stopped rmp-api", "stopped openclaw-gateway, rmp-worker", f"main moved {old}..{head[:12]}", f"started {units}",
        "stopped rmp-api", "stopped openclaw-gateway, rmp-worker", f"main moved {head[:12]}..{revert}",
        f"started {units}"]
    # The second count comes between the API's stop and the others'.
    assert lines.index("stopped rmp-api") < lines.index("stopped openclaw-gateway, rmp-worker")

    repos.old = git(repos.live, "rev-parse", "main")
    head = repos.change({"app/a.py": "A = 3\n"}, on=repos.old)
    lines.clear()
    assert deploy.self_deploy(spec(repos, head), FakeHost(repos.live, busy=[0, 1]), log=lines.append) is None
    assert lines[lines.index("stopped rmp-api"):] == ["stopped rmp-api", "waiting: 1 active user task(s)",
                                                       "started rmp-api again"]


@pytest.mark.parametrize("waits_at", ["the reserve", "the final count"])
def test_a_resumed_run_still_at_old_that_waits_goes_back_to_its_phase(repos, systemd_run, waits_at):
    head = repos.change({"app/a.py": "A = 2\n"})
    deploy.queue(reviewed(repos, head=head))
    git(repos.live, "push", "-q", "origin", f"{head}:refs/heads/main")
    pr = {"number": 7, "url": "https://github.com/Hyper-AI-Lab/openclaw-jev/pull/7"}
    deploy.write_json(deploy.state_file("t1"), {**state(), "phase": "applying", "pr": pr, "merge": head,
                                                "old": repos.old, "head": head, "affected": ["rmp-api", "rmp-worker"],
                                                "baseline": []})
    host = FakeHost(repos.live, busy=[1] if waits_at == "the final count" else [])
    if waits_at == "the reserve":
        host.elapsed = lambda: deploy.DEPLOY_UNIT_MAX_SEC
    assert deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None) is None
    assert state()["phase"] == "landed" and git(repos.live, "rev-parse", "main") == repos.old
    assert [c for c in host.calls if c[0] == "systemctl"] == [("systemctl", "start", "rmp-api", "rmp-worker")]
    # Before the change again, so a hand abort ends it.
    assert deploy.abort_run("t1").startswith("Aborted the deploy of t1 in phase landed")


def test_a_rollback_over_a_merge_commit_restores_the_old_tree(repos):
    """GitHub's main carries merge commits (a revert pull request merges with one), which git revert cannot take in a
    range; the rollback's revert commit is the old tree itself."""
    work = repos.tmp / "github-work"
    git(repos.tmp, "clone", "-q", "-b", "main", str(repos.origin), str(work))
    git(work, "checkout", "-q", "-b", "side")
    write(work, {"docs/d.md": "a side change\n"})
    commit(work, "a side change")
    git(work, "checkout", "-q", "main")
    write(work, {"app/a.py": "A = 2\n"})
    commit(work, "a change on main")
    git(work, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff", "-m", "Merge the side change", "side")
    head = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", "origin", "main")
    assert len(git(work, "rev-list", "--parents", "-n", "1", head).split()) == 3

    host = FakeHost(repos.live, checks=[{"failure": "health failed", "canary": None}])
    result = deploy.self_deploy(main_spec(head), host, log=lambda m: None)
    assert result["status"] == "rolled_back" and git(repos.live, "rev-parse", "main") == result["revert"]
    assert git(repos.live, "rev-parse", "main^{tree}") == git(repos.live, "rev-parse", f"{repos.old}^{{tree}}")
    assert git(repos.live, "rev-parse", "main^") == head
    assert [c for c in host.calls if c[0] == "land"] == [("land", f"aura/revert-{head[:12]}", "merge")]


def test_aborting_a_run_that_its_stop_ends_says_how_it_ended(repos, systemd_run, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    deploy.queue(main_spec("c" * 40))
    deploy.write_json(deploy.state_file(deploy.MAIN), {**state(deploy.MAIN), "attempt": 2})
    path = deploy.spec_file(deploy.MAIN)

    def the_unit_ends_on_sigterm():
        # The running unit, before the change: the stop's SIGTERM is its third attempt, which it records.
        unit.record(str(path), json.loads(path.read_text()), deploy.record_attempt(deploy.MAIN, "SIGTERM"))

    systemd_run.on_stop = the_unit_ends_on_sigterm
    summary = "The deploy failed 3 times before changing anything (last: SIGTERM); main is unchanged."
    assert deploy.abort_run(deploy.MAIN) == f"Stopped aura-deploy-main; the stop ended the run: failed: {summary}"
    assert json.loads(path.with_name("result.json").read_text())["summary"] == summary
    assert not deploy.state_file(deploy.MAIN).exists() and len(seen.notes) == 1


class KilledWithoutAWord(BaseException):
    """Stands in for SIGKILL or the OOM killer: no handler of the unit sees it, so its main never ends the launch."""


class KillingHost(FakeHost):
    """Killed without a word at its first systemctl call, its readiness baseline or CI's wait, as ``kill_in`` names."""

    def __init__(self, live, *, kill_in, **kwargs):
        super().__init__(live, **kwargs)
        self.kill_in = kill_in

    def die(self, step):
        if self.kill_in == step:
            self.kill_in = None
            raise KilledWithoutAWord(step)

    def systemctl(self, *args):
        self.die("systemctl")
        super().systemctl(*args)

    def readiness_report(self):
        self.die("baseline")
        return super().readiness_report()

    def wait_check(self, sha):
        self.die("wait_check")
        return super().wait_check(sha)


def killed_after_check_in(task, the_spec, host):
    """One launch whose unit checks in, then is killed without a word: its main's own end never runs."""
    assert deploy.mark_started(task)
    with pytest.raises(KilledWithoutAWord):
        deploy.self_deploy(the_spec, host, log=lambda m: None)


@pytest.mark.parametrize("phase", ["merged", "reverting"])
async def test_a_run_killed_without_a_word_three_times_mid_change_is_stuck_once(repos, systemd_run, reporting, phase):
    head = repos.change({"app/a.py": "A = 2\n"})
    git(repos.live, "merge", "-q", "--ff-only", head)
    deploy.queue(reviewed(repos, head=head))
    deploy.write_json(deploy.state_file("t1"), {
        **state(), "phase": phase, "old": repos.old, "head": head, "affected": ["rmp-api", "rmp-worker"],
        "baseline": [], **({"failure": "CANARY FAIL: task failed"} if phase == "reverting" else {})})
    deploy.queue(main_spec("c" * 40))
    for _ in range(3):
        assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-t1"
        killed_after_check_in("t1", reviewed(repos, head=head), KillingHost(repos.live, kill_in="systemctl"))
    assert deploy.next_deploy(FakeHost(repos.live)) is None and len(systemd_run.argvs) == 3
    assert state()["escapes"] == 2 and state()["attempt"] == 0

    await reporting.report()
    [(note, incident)] = reporting.notes
    assert note.startswith("Aura's deploy: stuck.") and f"ended 3 times in phase {phase}" in note
    assert state()["phase"] == phase and state()["dead_noted"] is True and state()["old"] == repos.old
    await reporting.report()
    assert len(reporting.notes) == 1 and deploy.next_deploy(FakeHost(repos.live)) is None
    assert len(systemd_run.argvs) == 3


@pytest.mark.parametrize("source", ["github", "reviewed"])
async def test_a_run_killed_without_a_word_three_times_before_the_change_ends_failed_once(repos, systemd_run,
                                                                                        reporting, source):
    if source == "github":
        head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
        task, the_spec, step = deploy.MAIN, main_spec(head), "baseline"
    else:
        head = repos.change({"app/a.py": "A = 2\n"})
        task, the_spec, step = "t1", reviewed(repos, head=head), "wait_check"
    deploy.queue(the_spec)
    for _ in range(3):
        assert deploy.next_deploy(FakeHost(repos.live)) == f"aura-deploy-{task}"
        killed_after_check_in(task, the_spec, KillingHost(repos.live, kill_in=step))
    assert deploy.next_deploy(FakeHost(repos.live)) is None and len(systemd_run.argvs) == 3

    await reporting.report()
    log = deploy.RUNS_DIR / task / "deploy.log"
    summary = (f"The deploy unit ended 3 times before it finished (see {log}), so nothing was deployed; main is "
               "unchanged.")
    result = json.loads(deploy.spec_file(task).with_name("result.json").read_text())
    assert result["status"] == "failed" and git(repos.live, "rev-parse", "main") == repos.old
    assert not deploy.state_file(task).exists()
    if source == "github":
        assert result["summary"] == summary
        [(note, incident)] = reporting.notes
        assert note.startswith("Aura's deploy: failed.") and summary in note
        reporting.client.start_workflow.assert_not_awaited()
    else:
        assert result["summary"] == f"{summary} GitHub's main has the change; it goes live with GitHub's main."
        assert reporting.notes == []
        shipped = reporting.client.start_workflow.await_args.args[1]["report"]["shipped"]
        assert shipped == result
    await reporting.report()
    assert len(reporting.events) == 1


@pytest.mark.parametrize("waits_on", ["the code-reload lock", "a busy count"])
def test_a_resumed_launch_that_only_waits_is_not_counted(repos, systemd_run, monkeypatch, waits_on):
    unit, seen = deploy_unit(monkeypatch)
    head = repos.change({"app/a.py": "A = 2\n"})
    git(repos.live, "push", "-q", "origin", f"{head}:refs/heads/main")
    deploy.queue(reviewed(repos, head=head))
    pr = {"number": 7, "url": "https://github.com/Hyper-AI-Lab/openclaw-jev/pull/7"}
    if waits_on == "the code-reload lock":
        git(repos.live, "merge", "-q", "--ff-only", head)
        phase, busy = "merged", []
    else:
        phase, busy = "applying", [1] * 8
    deploy.write_json(deploy.state_file("t1"), {**state(), "phase": phase, "pr": pr, "merge": head, "old": repos.old,
                                                "head": head, "affected": ["rmp-api", "rmp-worker"], "baseline": []})
    monkeypatch.setattr(deploy, "Host", lambda: FakeHost(repos.live, busy=busy))
    monkeypatch.setattr(deploy, "LOCK_WAIT_SEC", 0)
    with open(deploy.CODE_RELOAD_LOCK, "w") as lock:
        if waits_on == "the code-reload lock":
            fcntl.flock(lock, fcntl.LOCK_EX)
        for launch in range(1, 5):
            assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-t1"
            assert unit.main(str(deploy.spec_file("t1"))) == 0
            run = state()
            assert run["launches"] == run["started"] == run["ended"] == launch
            assert run["attempt"] == run["escapes"] == 0
    assert deploy.end_dead_runs(FakeHost(repos.live)) == [] and seen.added == []


def test_a_stopping_deploy_unit_counts_as_running(repos, systemd_run):
    class ListingHost(FakeHost):
        """Lists the deploy units the way the host does, through systemctl."""

        deploy_units = deploy.Host.deploy_units

    deploy.queue(reviewed(repos))
    launched("t1", 3, started=0)
    deploy.queue(main_spec("c" * 40))
    before = deploy.state_file("t1").read_text()
    # Its unit is still in its SIGTERM handler or its ExecStopPost=: the run is left alone, and nothing launches.
    systemd_run.units = [("aura-deploy-t1.service", "deactivating")]
    host = ListingHost(repos.live)
    assert host.deploy_units() == ["aura-deploy-t1.service"]
    assert deploy.gate(host) == ("a deploy unit is running", False)
    assert deploy.end_dead_runs(host) == [] and deploy.next_deploy(host) is None
    assert deploy.state_file("t1").read_text() == before and systemd_run.argvs == []

    systemd_run.units = [("aura-deploy-t1.service", "inactive")]
    assert [task for task, _, _ in deploy.end_dead_runs(host)] == ["t1"]
    assert deploy.next_deploy(host) == "aura-deploy-main"


@pytest.mark.parametrize("phase", ["queued", "restarted"])
async def test_an_exception_escaping_the_launch_three_times_ends_the_run(repos, systemd_run, reporting, monkeypatch,
                                                                         phase):
    unit, seen = deploy_unit(monkeypatch)
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    git(repos.live, "merge", "-q", "--ff-only", head)
    deploy.queue(main_spec(head))
    if phase == "restarted":
        deploy.write_json(deploy.state_file(deploy.MAIN), {**state(deploy.MAIN), "phase": "restarted",
                                                           "old": repos.old, "head": head, "affected": ["rmp-api"],
                                                           "baseline": []})
    # Before the change, GitHub's head is live already (unchanged); restarted, the checks pass (deployed). Either way
    # the result cannot be written, and the error escapes the unit with its state still there.
    real_write_json, full = deploy.write_json, [True]

    def write_json(path, data):
        if full and path.name == "result.json":
            raise OSError(28, "No space left on device")
        real_write_json(path, data)

    monkeypatch.setattr(deploy, "write_json", write_json)
    for _ in range(3):
        assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-main"
        with pytest.raises(OSError, match="No space left"):
            unit.main(str(deploy.spec_file(deploy.MAIN)))
    assert deploy.next_deploy(FakeHost(repos.live)) is None and len(systemd_run.argvs) == 3

    full.clear()
    await reporting.report()
    [(note, incident)] = reporting.notes
    if phase == "queued":
        assert "The deploy unit ended 3 times before it finished" in note and not deploy.state_file(deploy.MAIN).exists()
        assert json.loads(deploy.spec_file(deploy.MAIN).with_name("result.json").read_text())["status"] == "failed"
    else:
        assert note.startswith("Aura's deploy: stuck.") and state(deploy.MAIN)["dead_noted"] is True


@pytest.mark.parametrize("counted", ["escapes", "attempt"])
def test_a_kill_right_after_a_death_is_counted_counts_it_once(repos, systemd_run, monkeypatch, counted):
    unit, seen = deploy_unit(monkeypatch)
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    deploy.queue(main_spec(head))
    if counted == "escapes":
        git(repos.live, "merge", "-q", "--ff-only", head)
        deploy.write_json(deploy.state_file(deploy.MAIN), {**state(deploy.MAIN), "phase": "merged", "old": repos.old,
                                                           "head": head, "affected": ["rmp-api"], "baseline": []})
        host = FakeHost(repos.live, kill_at=("stop", 1))
    else:
        host = BlippingHost(repos.live, baselines=[RuntimeError("could not connect to server")])
    monkeypatch.setattr(deploy, "Host", lambda: host)
    real_write_json, armed = deploy.write_json, [True]

    def write_json(path, data):
        real_write_json(path, data)
        if armed and path.name == "deploy-state.json" and data.get(counted) == 1:
            armed.clear()
            raise KilledWithoutAWord("right after the death was counted")

    monkeypatch.setattr(deploy, "write_json", write_json)
    assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-main"
    with pytest.raises(KilledWithoutAWord):
        unit.main(str(deploy.spec_file(deploy.MAIN)))
    assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-main"
    assert state(deploy.MAIN)[counted] == 1 and state(deploy.MAIN)["escapes" if counted == "attempt" else "attempt"] == 0


def test_a_removed_run_state_is_never_recreated(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    deploy.queue(reviewed(repos, head=head))

    class AbortedWhileLanding(FakeHost):
        """The run's state is removed while the unit lands, as an end or an abort by hand would."""

        def land(self, commit_sha, branch, title, body, method):
            deploy.state_file("t1").unlink()
            return super().land(commit_sha, branch, title, body, method)

    result = deploy.self_deploy(reviewed(repos, head=head), AbortedWhileLanding(repos.live), log=lambda m: None)
    assert result["status"] == "deployed" and not deploy.state_file("t1").exists()


@pytest.fixture
def readiness(monkeypatch):
    """The real checks after a deploy (``deploy_checks.run``), with RMP's health and the gateway answering, readiness
    reporting ``after`` (set by the test) once the change is live, and the canary passing unless ``canaries`` holds
    its lines, in turn."""
    from app.coding import deploy_checks

    seen = SimpleNamespace(after={}, canaries=[])
    monkeypatch.setattr(deploy_checks, "_wait_ok", lambda url, seconds: True)
    monkeypatch.setattr(deploy_checks, "readiness_report", lambda: dict(seen.after))
    monkeypatch.setattr(deploy_checks, "canary", lambda live: seen.canaries.pop(0) if seen.canaries else "ok")
    return seen


class ReadinessHost(FakeHost):
    """Takes ``before`` as its readiness baseline and runs the real checks."""

    def __init__(self, live, *, before, **kwargs):
        super().__init__(live, **kwargs)
        self.before = before

    def readiness_report(self):
        return dict(self.before)

    def checks(self, restarted, baseline, *, ignore=()):
        self.calls.append(("checks", tuple(restarted), tuple(baseline), *([tuple(ignore)] if ignore else [])))
        return super(FakeHost, self).checks(restarted, baseline, ignore=ignore)


def ship_main(repos, monkeypatch, host):
    """Deploy a change to app/ on GitHub's main through the deploy unit on ``host``: its exit status, the result it
    recorded, and what it recorded and said (``deploy_unit``)."""
    unit, seen = deploy_unit(monkeypatch)
    monkeypatch.setattr(deploy, "Host", lambda: host)
    deploy.queue(main_spec(merged_on_github(repos, {"app/a.py": "A = 2\n"})))
    path = deploy.spec_file(deploy.MAIN)
    code = unit.main(str(path))
    return code, json.loads(path.with_name("result.json").read_text()), seen


NEW_FAILING = ("New or tightened readiness checks fail on this host: backup_manifest; they did not count against the "
               "deploy.")


def test_a_new_failing_check_is_reported_not_reverted(repos, monkeypatch, readiness):
    readiness.after = {"telemetry": "pass", "backup_manifest": "fail"}
    host = ReadinessHost(repos.live, before={"telemetry": "pass"})
    code, result, seen = ship_main(repos, monkeypatch, host)
    assert code == 0 and result["status"] == "deployed" and result["new_failing"] == ["backup_manifest"]
    assert NEW_FAILING in result["summary"] and git(repos.live, "rev-parse", "main") == result["head"]
    assert not [c for c in host.calls if c[0] == "land"]
    [(note, incident)] = seen.notes
    assert note.startswith("Aura's change is live.") and NEW_FAILING in note
    assert incident == f"deploy:main:{result['head'][:12]}"


def test_a_tightened_check_is_reported_not_reverted(repos, monkeypatch, readiness):
    readiness.after = {"telemetry": "pass", "backup_manifest": "fail"}
    host = ReadinessHost(repos.live, before={"telemetry": "pass", "backup_manifest": "warn"})
    code, result, seen = ship_main(repos, monkeypatch, host)
    assert code == 0 and result["status"] == "deployed" and result["new_failing"] == ["backup_manifest"]
    assert NEW_FAILING in result["summary"] and not [c for c in host.calls if c[0] == "land"]
    [(note, _)] = seen.notes
    assert NEW_FAILING in note


def test_a_passing_check_that_fails_rolls_back(repos, readiness):
    readiness.after = {"telemetry": "pass", "backup_manifest": "fail"}
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    host = ReadinessHost(repos.live, before={"telemetry": "pass", "backup_manifest": "pass"})
    result = deploy.self_deploy(main_spec(head), host, log=lambda m: None)
    assert result["status"] == "rolled_back" and result["error"] == "readiness checks failed: backup_manifest"
    assert [c for c in host.calls if c[0] == "land"] == [("land", f"aura/revert-{head[:12]}", "merge")]
    assert git(repos.live, "rev-parse", "main^{tree}") == git(repos.live, "rev-parse", f"{repos.old}^{{tree}}")


def test_a_check_failing_before_and_after_is_ignored(repos, monkeypatch, readiness):
    readiness.after = {"telemetry": "fail", "slack": "pass"}
    host = ReadinessHost(repos.live, before={"telemetry": "fail", "slack": "pass"})
    code, result, seen = ship_main(repos, monkeypatch, host)
    assert code == 0 and result["status"] == "deployed" and result["new_failing"] == []
    assert "readiness checks fail" not in result["summary"] and seen.notes == []


def test_a_canary_that_times_out_in_both_checks_still_publishes_the_revert(repos):
    class TimingOutHost(FakeHost):
        """Its checks raise the canary's timeout, after the change and again after the revert."""

        def checks(self, restarted, baseline, **kwargs):
            super().checks(restarted, baseline, **kwargs)
            raise subprocess.TimeoutExpired(["bash", "ops/canary.sh"], 600)

    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    host = TimingOutHost(repos.live)
    result = deploy.self_deploy(main_spec(head), host, log=lambda m: None)
    assert result["status"] == "rolled_back" and len([c for c in host.calls if c[0] == "checks"]) == 2
    assert [c for c in host.calls if c[0] == "land"] == [("land", f"aura/revert-{head[:12]}", "merge")]
    timeout = "Command '['bash', 'ops/canary.sh']' timed out after 600 seconds"
    assert f"the checks still fail: the checks failed ({timeout})" in result["summary"]
    assert git(repos.live, "rev-parse", "main") == result["revert"] == git(repos.origin, "rev-parse", "main")


def test_a_revert_window_that_fails_still_publishes_the_revert_and_says_so(repos):
    class FailingBack(FakeHost):
        """The revert window's stop of rmp-api fails."""

        def systemctl(self, *args):
            super().systemctl(*args)
            if self.calls.count(("systemctl", "stop", "rmp-api")) == 2 and args == ("stop", "rmp-api"):
                raise subprocess.CalledProcessError(1, ["systemctl", *args])

    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    host = FailingBack(repos.live, checks=[{"failure": "CANARY FAIL: task failed", "canary": None}])
    result = deploy.self_deploy(main_spec(head), host, log=lambda m: None)
    assert result["status"] == "rolled_back"
    assert (f"; reverting main to {result['revert'][:12]} and restarting rmp-api, rmp-worker failed (Command "
            "'['systemctl', 'stop', 'rmp-api']' returned non-zero exit status 1.); Aura is healthy again; GitHub's "
            "main is reverted too") in result["summary"]
    # The window started what it stopped; the publish and the re-check still ran.
    assert [c for c in host.calls if c[0] in ("systemctl", "checks", "land")][-3:] == [
        ("systemctl", "start", "rmp-api", "rmp-worker"), ("land", f"aura/revert-{head[:12]}", "merge"),
        ("checks", ("rmp-api", "rmp-worker"), ("telemetry",))]
    assert git(repos.origin, "rev-parse", "main") == result["revert"]


@pytest.mark.parametrize("publish", ["blocked", "failed", "merged"])
def test_a_rollback_reports_healthy_again_when_only_the_github_ancestor_clause_fails(repos, monkeypatch, readiness,
                                                                                     publish):
    from app.coding import deploy_checks

    def report():
        # claude_direct's clause that fails until GitHub's main holds the revert: GitHub's main contains the live main.
        contained = subprocess.run(["git", "merge-base", "--is-ancestor", git(repos.live, "rev-parse", "main"), "main"],
                                   cwd=repos.origin, capture_output=True).returncode == 0
        return {"telemetry": "pass", "claude_direct": "pass" if contained else "fail"}

    monkeypatch.setattr(deploy_checks, "readiness_report", report)
    readiness.canaries = ["CANARY FAIL: task failed"]
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    # The revert's pull request waits for a label, so its test check fails; or GitHub cannot be reached; or it merges.
    lands = {"blocked": ["failure"], "failed": [deploy.github.GitHubError("POST repos/x/pulls: 502 Bad Gateway")],
             "merged": []}[publish]
    host = ReadinessHost(repos.live, before=report(), lands=lands)
    result = deploy.self_deploy(main_spec(head), host, log=lambda m: None)
    assert result["status"] == "rolled_back" and result["error"] == "CANARY FAIL: task failed"
    calls = [c for c in host.calls if c[0] in ("checks", "land")]
    units = ("rmp-api", "rmp-worker")
    if publish == "blocked":
        url = result["revert_pr"]["pr"]["url"]
        assert f"; Aura is healthy again; on GitHub the revert waits in {url} (its test check is failure)." in (
            result["summary"])
        assert calls == [("checks", units, ("telemetry", "claude_direct")),
                         ("land", f"aura/revert-{head[:12]}", "merge"),
                         ("checks", units, ("telemetry", "claude_direct"), ("claude_direct",))]
    elif publish == "failed":
        assert ("; Aura is healthy again; the revert is not on GitHub yet (POST repos/x/pulls: 502 Bad Gateway)."
                in result["summary"])
        assert calls == [("checks", units, ("telemetry", "claude_direct")),
                         ("land", f"aura/revert-{head[:12]}", "merge"),
                         ("checks", units, ("telemetry", "claude_direct"), ("claude_direct",))]
    else:
        assert "; Aura is healthy again; GitHub's main is reverted too" in result["summary"]
        # The re-check comes after the publish, with nothing left out once the revert merged.
        assert calls == [("checks", units, ("telemetry", "claude_direct")),
                         ("land", f"aura/revert-{head[:12]}", "merge"),
                         ("checks", units, ("telemetry", "claude_direct"))]


class KilledInTheReCheck(FakeHost):
    """Killed in its second checks, the re-check after the revert was published."""

    def checks(self, restarted, baseline, **kwargs):
        result = super().checks(restarted, baseline, **kwargs)
        if len([c for c in self.calls if c[0] == "checks"]) == 2:
            raise deploy.Interrupted("SIGTERM")
        return result


def test_a_rollback_killed_after_its_publish_does_not_publish_again(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    deploy.queue(reviewed(repos, head=head))
    host = KilledInTheReCheck(repos.live, checks=[{"failure": "CANARY FAIL: task failed", "canary": None}])
    with pytest.raises(deploy.Interrupted):
        deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
    run = state()
    assert run["phase"] == "reverting" and run["published"]["status"] == "merged"
    assert git(repos.origin, "rev-parse", "main") == run["revert"]

    host = FakeHost(repos.live)
    result = deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
    assert result["status"] == "rolled_back" and result["revert_pr"] == run["published"]
    assert "Aura is healthy again; GitHub's main is reverted too" in result["summary"]
    assert not [c for c in host.calls if c[0] == "land"]


@pytest.mark.parametrize("first", [deploy.github.GitHubError("POST repos/x/pulls: 502 Bad Gateway"), "failure"],
                         ids=["error", "waiting"])
def test_a_resumed_rollback_publishes_again_only_after_a_publish_that_failed_with_an_error(repos, first):
    head = repos.change({"app/a.py": "A = 2\n"})
    deploy.queue(reviewed(repos, head=head))
    host = KilledInTheReCheck(repos.live, checks=[{"failure": "CANARY FAIL: task failed", "canary": None}],
                              lands=["merged", first])
    with pytest.raises(deploy.Interrupted):
        deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
    run = state()
    assert run["phase"] == "reverting" and run["published"]["status"] == ("failed" if first != "failure" else "blocked")
    assert git(repos.origin, "rev-parse", "main") == head

    host = FakeHost(repos.live)
    result = deploy.self_deploy(reviewed(repos, head=head), host, log=lambda m: None)
    lands = [c for c in host.calls if c[0] == "land"]
    if first == "failure":
        # The revert's pull request is on GitHub, waiting: it is not opened again.
        assert lands == [] and result["revert_pr"] == run["published"]
        assert f"on GitHub the revert waits in {run['published']['pr']['url']}" in result["summary"]
    else:
        assert lands == [("land", f"aura/revert-{head[:12]}", "merge")] and result["revert_pr"]["status"] == "merged"
        assert "GitHub's main is reverted too" in result["summary"]
        assert git(repos.origin, "rev-parse", "main") == run["revert"]
    assert result["status"] == "rolled_back"


@pytest.fixture
def pauses(monkeypatch):
    """The pauses between the tries of Kirill's note, recorded instead of slept."""
    seen = []

    async def sleep(seconds):
        seen.append(seconds)

    monkeypatch.setattr(deploy.asyncio, "sleep", sleep)
    return seen


def run_noting_unit(tmp_path, monkeypatch, answers, *, source="github", commit=None):
    """Run the deploy unit for a run that rolls back, whose tries of Kirill's note answer ``answers`` in turn (an
    exception in it is raised) and whose database commit raises ``commit`` when given: the unit's exit status, the
    order of its steps and what it recorded and started (``deploy_unit``)."""
    unit, seen = deploy_unit(monkeypatch)
    monkeypatch.setattr(unit.deploy, "CODE_RELOAD_LOCK", tmp_path / "code-reload.lock")
    monkeypatch.setattr(unit.deploy, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(unit.deploy, "self_deploy", lambda the_spec, host, *, log: {
        "status": "rolled_back", "head": "c" * 40, "commits": ["Fix the greeting (#12)"], "summary": "Rolled back."})
    host = FakeHost(tmp_path)
    monkeypatch.setattr(unit.deploy, "Host", lambda: host)
    steps, answers = [], list(answers)

    async def notify(message, *, incident_id):
        steps.append("note")
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(unit, "notify_ops_slack", notify)
    seen.db.add = lambda event: steps.append("event") or seen.added.append(event)
    if commit:
        seen.db.commit = AsyncMock(side_effect=commit)
    seen.client.start_workflow = AsyncMock(side_effect=lambda *args, **kwargs: steps.append("reply"))
    the_spec = main_spec("c" * 40) if source == "github" else {
        **reviewed(SimpleNamespace(old="b" * 40)), "context": {"task_id": "t1"}}
    deploy.queue(the_spec)
    code = unit.main(str(deploy.spec_file(the_spec["task_id"])))
    return code, steps, seen


def test_the_note_is_retried_and_logged_as_it_went(tmp_path, monkeypatch, capsys, pauses):
    code, steps, seen = run_noting_unit(tmp_path, monkeypatch, [False, False, True])
    out = capsys.readouterr().out
    # The note goes first; the event is written once it was delivered.
    assert code == 1 and steps == ["note", "note", "note", "event"] and pauses == [30, 120]
    assert out.count("told Kirill") == 1 and "not delivered" not in out
    assert seen.added[0].event_payload["note_delivered"] is True
    assert seen.added[0].event_payload["status"] == "rolled_back"


def test_the_note_goes_out_when_the_event_cannot_be_written(tmp_path, monkeypatch, capsys, pauses):
    code, steps, seen = run_noting_unit(tmp_path, monkeypatch, [True], commit=RuntimeError("could not connect"))
    out = capsys.readouterr().out
    assert code == 1 and steps == ["note", "event"] and pauses == [] and "told Kirill" in out
    assert "recording the result on the task failed (could not connect)" in out
    assert json.loads((tmp_path / "runs" / "main" / "result.json").read_text())["status"] == "rolled_back"

    # A reviewed job's reply run starts all the same; it sends no note of its own.
    code, steps, seen = run_noting_unit(tmp_path, monkeypatch, [], source="reviewed",
                                        commit=RuntimeError("could not connect"))
    assert steps == ["event", "reply"] and seen.added[0].event_payload["status"] == "rolled_back"
    assert "note_delivered" not in seen.added[0].event_payload


def test_a_reply_run_that_cannot_start_is_logged_once_the_result_is_recorded(tmp_path, monkeypatch, capsys, pauses):
    unit, seen = deploy_unit(monkeypatch)
    monkeypatch.setattr(unit.deploy, "CODE_RELOAD_LOCK", tmp_path / "code-reload.lock")
    monkeypatch.setattr(unit.deploy, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(unit.deploy, "self_deploy", lambda the_spec, host, *, log: {
        "status": "deployed", "head": "c" * 40, "summary": "Deployed cccccccccccc."})
    monkeypatch.setattr(unit.deploy, "Host", lambda: FakeHost(tmp_path))
    monkeypatch.setattr(unit, "connect_temporal", AsyncMock(side_effect=RuntimeError("Temporal is unavailable")))
    the_spec = {**reviewed(SimpleNamespace(old="b" * 40)), "context": {"task_id": "t1"}}
    deploy.queue(the_spec)
    assert unit.main(str(deploy.spec_file("t1"))) == 0
    assert "starting the reply run workflow-t1 failed (Temporal is unavailable)" in capsys.readouterr().out
    assert seen.added[0].event_payload["status"] == "deployed" and not deploy.state_file("t1").exists()


def test_an_undelivered_note_is_logged_as_undelivered(tmp_path, monkeypatch, capsys, pauses):
    code, steps, seen = run_noting_unit(tmp_path, monkeypatch, [False, RuntimeError("Slack is down"), False])
    out = capsys.readouterr().out
    assert steps == ["note", "note", "note", "event"] and pauses == [30, 120]
    assert "told Kirill" not in out and "the note to Kirill was not delivered after 3 tries" in out
    assert "a try of the note to Kirill failed (Slack is down)" in out
    assert seen.added[0].event_payload["note_delivered"] is False


async def test_the_reports_note_goes_first_on_its_own_and_is_retried(repos, systemd_run, reporting, pauses,
                                                                      monkeypatch, caplog):
    from app.production import ops_notify

    deploy.queue(main_spec("c" * 40))
    launched(deploy.MAIN, 3, started=0)
    steps, answers = [], [False, True]

    async def notify(message, *, incident_id):
        steps.append("note")
        return answers.pop(0)

    monkeypatch.setattr(ops_notify, "notify_ops_slack", notify)
    reporting.db.add = lambda event: steps.append("event") or reporting.events.append(event)
    reporting.db.commit = AsyncMock(side_effect=RuntimeError("could not connect"))
    caplog.set_level(logging.INFO, logger="rmp.reconciler")
    await reporting.report()
    assert steps == ["note", "note", "event"] and pauses == [30]
    assert reporting.events[0].event_payload["note_delivered"] is True
    assert "Recording the ended deploy of main failed: could not connect" in caplog.text
    # A delivered note is logged as information, not as a warning.
    told = [r for r in caplog.records if r.getMessage() == "The ended deploy of main: told Kirill"]
    assert [r.levelno for r in told] == [logging.INFO]


async def test_a_report_whose_note_cannot_be_written_records_it_undelivered(repos, systemd_run, reporting,
                                                                         monkeypatch, caplog):
    def unwritable(result, repo):
        raise KeyError("repositories")

    monkeypatch.setattr(deploy, "deploy_note", unwritable)
    deploy.queue(main_spec("c" * 40))
    launched(deploy.MAIN, 3, started=0)
    await reporting.report()
    [event] = reporting.events
    assert reporting.notes == [] and event.event_payload["note_delivered"] is False
    assert event.event_payload["status"] == "failed"
    assert "Telling Kirill about the ended deploy of main failed: 'repositories'" in caplog.text


def reload_lock_held():
    """Whether the code-reload lock is held, here or by another process: a probe through a descriptor of its own."""
    with open(deploy.CODE_RELOAD_LOCK, "w") as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


@pytest.mark.parametrize("early, status", [("unchanged", "unchanged"), ("blocked", "blocked"),
                                           ("main moved", "failed"), ("CI on the merge commit", "blocked"),
                                           ("the third attempt", "failed")])
def test_an_early_result_waits_while_a_restore_holds_the_lock(repos, monkeypatch, early, status):
    """A result reached before the deploy takes the code-reload lock is recorded under it, taken at once: while a
    restore holds it, nothing is recorded or sent, and the run keeps its state."""
    unit, seen = deploy_unit(monkeypatch)
    error = deploy.github.GitHubError("GET repos/x/commits/y/check-runs: 502 Bad Gateway")
    if early == "unchanged":
        head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
        git(repos.live, "merge", "-q", "--ff-only", head)
        the_spec, host = main_spec(head), FakeHost(repos.live)
    else:
        head = repos.change({"app/a.py": "A = 2\n"})
        the_spec = reviewed(repos, head=head)
        host = {"blocked": FakeHost(repos.live, lands=["failure", "failure"]), "main moved": FakeHost(repos.live),
                "CI on the merge commit": FakeHost(repos.live, ci={head: "failure"}),
                "the third attempt": BlippingHost(repos.live, waits=[error, error])}[early]
        if early == "main moved":
            write(repos.live, {"docs/d.md": "another deploy\n"})
            commit(repos.live, "another deploy")
    monkeypatch.setattr(deploy, "Host", lambda: host)
    task, path = the_spec["task_id"], deploy.spec_file(the_spec["task_id"])
    deploy.queue(the_spec)
    if early == "the third attempt":
        deploy.write_json(deploy.state_file(task), {**state(task), "attempt": 2})

    with open(deploy.CODE_RELOAD_LOCK, "w") as restore:
        fcntl.flock(restore, fcntl.LOCK_EX)
        assert unit.main(str(path)) == 0
    assert not path.with_name("result.json").exists() and seen.added == [] and seen.notes == []
    assert seen.client.start_workflow.await_count == 0
    # The run keeps its state; a last attempt that found the lock held is not counted.
    kept = state(task)
    assert kept["attempt"] == (2 if early == "the third attempt" else 0)
    assert kept["phase"] == ("landed" if early in ("CI on the merge commit", "the third attempt") else "queued")

    held = []
    seen.db.add = lambda event: held.append(reload_lock_held()) or seen.added.append(event)
    unit.main(str(path))
    result = json.loads(path.with_name("result.json").read_text())
    assert result["status"] == status and held == [True] and not deploy.state_file(task).exists()
    assert not reload_lock_held()


def test_a_result_reached_under_the_lock_is_recorded_without_letting_go_of_it(repos, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    monkeypatch.setattr(deploy, "Host", lambda: FakeHost(repos.live))
    real_self_deploy, returned = deploy.self_deploy, []

    def self_deploy(*args, **kwargs):
        result = real_self_deploy(*args, **kwargs)
        returned.append(reload_lock_held())
        return result

    monkeypatch.setattr(deploy, "self_deploy", self_deploy)
    held = []
    seen.db.add = lambda event: held.append(reload_lock_held()) or seen.added.append(event)
    deploy.queue(main_spec(merged_on_github(repos, {"app/a.py": "A = 2\n"})))
    assert unit.main(str(deploy.spec_file(deploy.MAIN))) == 0
    # Still held when the deploy returned its result, and while it is recorded; free once the unit is done.
    assert returned == [True] and held == [True] and not reload_lock_held()
    assert seen.added[0].event_payload["status"] == "deployed"


def test_a_host_that_cannot_be_made_is_an_error_before_the_change(repos, monkeypatch, capsys):
    """Making the deploy's host reads the coding settings; a failure there is retried like any error before the change,
    and its last attempt is recorded under the code-reload lock, taken at once, or waits while a restore holds it."""
    unit, seen = deploy_unit(monkeypatch)

    def unreadable():
        raise ValueError("settings.json: Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(deploy, "Host", unreadable)
    deploy.queue(main_spec(merged_on_github(repos, {"app/a.py": "A = 2\n"})))
    path = deploy.spec_file(deploy.MAIN)
    assert unit.main(str(path)) == 1 and not path.with_name("result.json").exists() and seen.added == []
    run = state(deploy.MAIN)
    assert run["attempt"] == 1 and run["last_error"].startswith("settings.json: Expecting value")
    assert run["ended"] == run["started"] and "stopped before the change (settings.json" in capsys.readouterr().out

    with open(deploy.CODE_RELOAD_LOCK, "w") as restore:
        fcntl.flock(restore, fcntl.LOCK_EX)
        # An attempt before the last is counted while a restore holds the lock, and records nothing.
        assert unit.main(str(path)) == 1
        assert state(deploy.MAIN)["attempt"] == 2 and not path.with_name("result.json").exists() and seen.added == []
        # The last one waits, uncounted.
        assert unit.main(str(path)) == 0
    assert state(deploy.MAIN)["attempt"] == 2 and not path.with_name("result.json").exists() and seen.added == []

    held = []
    seen.db.add = lambda event: held.append(reload_lock_held()) or seen.added.append(event)
    assert unit.main(str(path)) == 1
    result = json.loads(path.with_name("result.json").read_text())
    assert result["status"] == "failed" and "(last: settings.json: Expecting value" in result["summary"]
    assert held == [True] and not deploy.state_file(deploy.MAIN).exists() and not reload_lock_held()


def test_a_land_whose_response_was_lost_is_found_merged_and_not_landed_again(repos, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    github = {}

    class LosesTheLandsAnswer(FakeHost):
        """GitHub merges the pull request, and its answer never arrives."""

        def land(self, commit_sha, branch, title, body, method):
            super().land(commit_sha, branch, title, body, method)
            raise deploy.github.GitHubError("PUT repos/x/pulls/3/merge: connection reset by peer")

    head = repos.change({"app/a.py": "A = 2\n"})
    hosts = [LosesTheLandsAnswer(repos.live, merged=github), FakeHost(repos.live, merged=github)]
    monkeypatch.setattr(deploy, "Host", lambda: hosts.pop(0))
    deploy.queue(reviewed(repos, head=head))
    path = deploy.spec_file("t1")
    first, relaunched = hosts

    assert unit.main(str(path)) == 1
    assert state()["phase"] == "queued" and state()["attempt"] == 1 and git(repos.origin, "rev-parse", "main") == head
    # The first try looks nothing up: it has no earlier try.
    assert [c for c in first.calls if c[0] in ("land", "merged_pull")] == [("land", "aura/t1-x", "squash")]

    assert unit.main(str(path)) == 0
    result = json.loads(path.with_name("result.json").read_text())
    assert result["status"] == "deployed" and result["pr"] == github["aura/t1-x"]["pr"] and result["head"] == head
    assert [c for c in relaunched.calls if c[0] in ("land", "merged_pull")] == [("merged_pull", "aura/t1-x")]
    assert git(repos.live, "rev-parse", "main") == head and ("wait_check", head) in relaunched.calls


def test_a_retried_land_that_github_never_merged_lands_again(repos, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    head = repos.change({"app/a.py": "A = 2\n"})
    host = FakeHost(repos.live, lands=[deploy.github.GitHubError("POST repos/x/pulls: 502 Bad Gateway")])
    monkeypatch.setattr(deploy, "Host", lambda: host)
    deploy.queue(reviewed(repos, head=head))
    path = deploy.spec_file("t1")
    assert unit.main(str(path)) == 1 and state()["attempt"] == 1
    assert unit.main(str(path)) == 0
    assert json.loads(path.with_name("result.json").read_text())["status"] == "deployed"
    assert [c for c in host.calls if c[0] in ("land", "merged_pull")] == [
        ("land", "aura/t1-x", "squash"), ("merged_pull", "aura/t1-x"), ("land", "aura/t1-x", "squash")]


def test_the_watcher_leaves_a_stopping_main_unit_its_run(repos, github_ci, systemd_run):
    class ListingHost(FakeHost):
        """Lists the deploy units the way the host does, through systemctl."""

        deploy_units = deploy.Host.deploy_units

    first = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    host = ListingHost(repos.live)
    assert deploy.watch_main(host) == "aura-deploy-main"
    queued = (deploy.spec_file(deploy.MAIN).read_text(), deploy.state_file(deploy.MAIN).read_text())
    later = repos.change({"app/a.py": "A = 3\n"}, on=first)
    git(repos.live, "push", "-q", "origin", f"{later}:refs/heads/main")
    # Its unit is still in its SIGTERM handler or its ExecStopPost=: the run it may still record is left as it is.
    systemd_run.units = [("aura-deploy-main.service", "deactivating")]
    assert deploy.watch_main(host) is None and len(systemd_run.argvs) == 1
    assert (deploy.spec_file(deploy.MAIN).read_text(), deploy.state_file(deploy.MAIN).read_text()) == queued

    systemd_run.units = [("aura-deploy-main.service", "inactive")]
    assert deploy.watch_main(host) == "aura-deploy-main" and len(systemd_run.argvs) == 2
    assert json.loads(deploy.spec_file(deploy.MAIN).read_text())["head"] == later


def test_a_note_that_cannot_be_written_still_lets_the_result_be_recorded(tmp_path, monkeypatch, capsys, pauses):
    def unwritable(result, repo):
        raise KeyError("repositories")

    monkeypatch.setattr(deploy, "deploy_note", unwritable)
    code, steps, seen = run_noting_unit(tmp_path, monkeypatch, [True])
    out = capsys.readouterr().out
    assert steps == ["event"] and "writing the note to Kirill failed ('repositories')" in out
    assert seen.added[0].event_payload["note_delivered"] is False


@pytest.mark.parametrize("slack, status", [("pass", "deployed"), ("fail", "rolled_back")])
def test_a_run_the_previous_deploy_code_left_compares_its_baseline_as_that_code_did(repos, readiness, slack, status):
    """The deploy code before this one recorded the readiness baseline as the list of the checks failing then. A run
    of it that this code resumes counts every other check as passed then: a check failing now and then is ignored, and
    one failing now only fails the deploy."""
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    git(repos.live, "merge", "-q", "--ff-only", head)
    deploy.queue(main_spec(head))
    deploy.write_json(deploy.state_file(deploy.MAIN), {
        **batch_1_state(1, started=1, escapes=1), "phase": "restarted", "old": repos.old, "head": head,
        "affected": ["rmp-api", "rmp-worker"], "baseline": ["telemetry"]})
    readiness.after = {"telemetry": "fail", "slack": slack}
    result = deploy.self_deploy(main_spec(head), ReadinessHost(repos.live, before={}), log=lambda m: None)
    assert result["status"] == status
    if status == "deployed":
        assert result["new_failing"] == [] and git(repos.live, "rev-parse", "main") == head
    else:
        assert result["error"] == "readiness checks failed: slack"


@pytest.mark.parametrize("publish", ["merged", "blocked"])
def test_a_rollback_the_previous_deploy_code_left_is_published_once_and_rechecked_as_that_code_compared(
        repos, readiness, publish):
    """A run the deploy code before this one was killed in while it reverted: the tree moved back to its recorded
    revert, its baseline the list of the checks failing then, and no publish recorded. This code finishes the revert
    window, publishes the revert once, then re-checks, counting every check missing from the list as passed then and
    leaving ``claude_direct`` out while the revert waits on GitHub."""
    head = merged_on_github(repos, {"app/a.py": "A = 2\n"})
    git(repos.live, "merge", "-q", "--ff-only", head)
    revert = git(repos.live, "-c", "user.name=RMP deploy", "-c", "user.email=rmp@aura.local", "commit-tree",
                 f"{repos.old}^{{tree}}", "-p", head, "-m", f"Revert {head[:12]}: the deploy's checks failed")
    git(repos.live, "merge", "-q", "--ff-only", revert)
    deploy.queue(main_spec(head))
    deploy.write_json(deploy.state_file(deploy.MAIN), {
        **batch_1_state(1, started=1, escapes=1), "phase": "reverting", "old": repos.old, "head": head,
        "affected": ["rmp-api", "rmp-worker"], "baseline": ["telemetry"], "failure": "CANARY FAIL: task failed",
        "revert": revert})
    readiness.after = {"telemetry": "fail", "claude_direct": "pass" if publish == "merged" else "fail"}
    host = ReadinessHost(repos.live, before={}, lands=["failure"] if publish == "blocked" else [])
    result = deploy.self_deploy(main_spec(head), host, log=lambda m: None)
    assert result["status"] == "rolled_back" and result["revert"] == revert and "Aura is healthy again" in (
        result["summary"])
    assert result["revert_pr"]["status"] == publish and git(repos.live, "rev-parse", "main") == revert
    assert [c for c in host.calls if c[0] in ("systemctl", "land", "checks")] == [
        *switched("rmp-api", "rmp-worker"), ("land", f"aura/revert-{head[:12]}", "merge"),
        ("checks", ("rmp-api", "rmp-worker"), ("telemetry",), *([("claude_direct",)] if publish == "blocked" else []))]
