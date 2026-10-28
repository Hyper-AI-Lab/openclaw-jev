"""Shipping approved changes against hermetic repos: the live repo, its GitHub remote, the review repository, mirrors.

git runs for real; the host's systemctl, pip, checks and the gate's probes are a recording fake, and so is the
launcher's systemd-run.
"""
import asyncio
import fcntl
import importlib.util
import json
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
    systemctl call of that verb: ``deploy.Interrupted`` is raised there, once, before the call is made."""

    def __init__(self, live, *, busy=(), checks=(), lands=(), units=(), held=False, ci=None, kill_at=None):
        super().__init__(live, repo="Hyper-AI-Lab/openclaw-jev")
        self.calls, self.busy, self.results, self.lands = [], list(busy), list(checks), list(lands)
        self.units, self.held, self.ci = list(units), held, dict(ci or {})
        self.kill_at = list(kill_at) if kill_at else None

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
        return {"status": "merged", "pr": pr, "merge": commit}

    def wait_check(self, sha):
        self.calls.append(("wait_check", sha))
        return self.ci.get(sha, "success")

    def readiness_baseline(self):
        return ["telemetry"]

    def checks(self, restarted, baseline):
        self.calls.append(("checks", tuple(restarted), tuple(baseline)))
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
    recorded, or raises while ``fail`` is set, ``units`` are the deploy units listed, and each unit stopped is recorded
    in ``stopped``. Every other command runs for real."""
    real, state = subprocess.run, SimpleNamespace(argvs=[], fail=False, units=[], stopped=[])

    def run(argv, **kwargs):
        if argv[:2] == ["systemctl", "list-units"]:
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout="".join(f"{unit} loaded active running Deploy\n" for unit in state.units))
        if argv[:2] == ["systemctl", "stop"]:
            state.stopped += argv[2:]
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
    github_ci.active, host.units = True, ["aura-deploy-main.service"]
    later = repos.change({"app/a.py": "A = 3\n"}, on=first)
    git(repos.live, "push", "-q", "origin", f"{later}:refs/heads/main")
    assert deploy.watch_main(host) is None and len(systemd_run.argvs) == 1
    github_ci.active, host.units = False, []
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
    monkeypatch.setattr(unit.deploy, "Host", lambda: None)
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
    """CI's test check on GitHub's main (``check``) and whether the deploy unit of GitHub's main runs (``active``)."""
    state = SimpleNamespace(check="success", active=False)
    monkeypatch.setattr(deploy.github, "check", lambda sha, repo: state.check)
    monkeypatch.setattr(deploy, "_unit_active", lambda unit: state.active)
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
    watching.check, watching.active = "success", True
    assert deploy.watch_main(host) is None
    watching.active = False
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
    github_ci.active, host = True, FakeHost(repos.live, units=["aura-deploy-main.service"])
    assert deploy.watch_main(host) is None and systemd_run.argvs == [] and deploy.queued_runs() == []

    for task, done in left.items():
        (deploy.RUNS_DIR / task / "result.json").write_text(json.dumps({"status": "deployed", "head": done}) + "\n")
    github_ci.active, host.units = False, []
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
    host = FakeHost(repos.live, busy=[0, 1])
    assert deploy.self_deploy(spec(repos, head), host, log=lambda m: None) is None
    assert [c for c in host.calls if c[0] in ("systemctl", "count")] == [
        ("count", 0), ("systemctl", "stop", "rmp-api"), ("count", 1), ("systemctl", "start", "rmp-api")]
    assert git(repos.live, "rev-parse", "main") == repos.old


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
    window = [] if phase == "restarted" else switched("rmp-api", "rmp-worker")
    started = [("systemctl", "start", "rmp-api", "rmp-worker")] if at == "old" else []
    # Only a run still at old counts again, before its window: a run whose tree moved finishes its window.
    counts = [("count", 0)] * 2 if at == "old" else []
    calls = [c for c in host.calls if c[0] in ("systemctl", "count", "land", "wait_check")]
    assert calls == ([*started, counts[0], window[0], counts[1], *window[1:]] if at == "old" else window)


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
    assert [c for c in host.calls if c[0] == "systemctl"] == [
        ("systemctl", "stop", "rmp-api", listed), ("systemctl", "stop", "openclaw-gateway", "rmp-worker", listed),
        ("systemctl", "start", "openclaw-gateway", "rmp-api", "rmp-worker", listed)]
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

    def readiness_baseline(self):
        if self.baselines:
            raise self.baselines.pop(0)
        return super().readiness_baseline()

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
    """The reconciler's report of ended deploy runs, with the database, Slack and Temporal faked: the events it added
    (``events``), the notes it sent (``notes``) and the reply runs it started (``client``); ``host`` is the deploy host
    it sees."""
    from app import config, reconciler
    from app.production import ops_notify

    seen = SimpleNamespace(events=[], notes=[], client=SimpleNamespace(start_workflow=AsyncMock()),
                           host=FakeHost(repos.live))
    db = SimpleNamespace(add=seen.events.append, get=AsyncMock(return_value=None), commit=AsyncMock())
    session = AsyncMock()
    session.__aenter__.return_value = db
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
        "coding.deploy", "deploy", "main", result)
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
    monkeypatch.setattr(deploy, "_unit_active", lambda unit: False)
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
    github_ci.active, host = True, FakeHost(repos.live, units=["aura-deploy-main.service"])
    assert deploy.end_dead_runs(host) == [] and deploy.watch_main(host) is None
    assert deploy.state_file(deploy.MAIN).read_text() == before and systemd_run.argvs == []

    git(repos.live, "merge", "-q", "--ff-only", head)
    deploy.write_json(deploy.spec_file(deploy.MAIN).with_name("result.json"), {"status": "deployed", "head": head})
    deploy.state_file(deploy.MAIN).unlink()
    github_ci.active, host.units = False, []
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
