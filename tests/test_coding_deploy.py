"""Shipping approved changes against hermetic repos: the live repo, its GitHub remote, the review repository, mirrors.

git runs for real; the host's systemctl, pip, checks and waiting are a recording fake.
"""
import fcntl
import importlib.util
import json
import subprocess
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
    (the idle counts in turn, then 0; an exception in it is raised)."""

    def __init__(self, live, *, busy=(), checks=(), lands=(), units=(), held=False):
        super().__init__(live, repo="Hyper-AI-Lab/openclaw-jev")
        self.calls, self.busy, self.results, self.lands = [], list(busy), list(checks), list(lands)
        self.units, self.held = list(units), held

    def active_user_tasks(self):
        count = self.busy.pop(0) if self.busy else 0
        if isinstance(count, Exception):
            raise count
        return count

    def deploy_units(self):
        return list(self.units)

    def reload_lock_held(self):
        return self.held

    def systemctl(self, *args):
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

    def readiness_baseline(self):
        return ["telemetry"]

    def checks(self, restarted, baseline):
        self.calls.append(("checks", tuple(restarted), tuple(baseline)))
        return self.results.pop(0) if self.results else {"failure": None, "canary": "ok"}


def spec(repos, head):
    return {"task_id": "t1", "old": repos.old, "head": head, "branch": "aura/t1-x", "title": "The approved change",
            "body": "Body"}


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
    assert [c for c in host.calls if c[0] in ("systemctl", "pip")] == [("systemctl", "restart", *services)]
    assert ("checks", services, ("telemetry",)) in host.calls


def test_requirements_are_installed_before_the_restart_and_unit_files_are_not(repos):
    head = repos.change({"requirements.txt": "x==2\n", "ops/systemd/aura-x.service": "[Service]\nNice=5\n"})
    host = FakeHost(repos.live)
    assert deploy.self_deploy(spec(repos, head), host, log=lambda m: None)["status"] == "deployed"
    assert [c for c in host.calls if c[0] in ("systemctl", "pip")] == [
        ("pip",), ("systemctl", "restart", "rmp-api", "rmp-worker")]


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
    assert [c for c in host.calls if c[0] == "systemctl"] == [("systemctl", "restart", "aura-web-backends", "rmp-api",
                                                               "rmp-worker")]


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
    assert [c for c in host.calls if c[0] == "systemctl"] == [("systemctl", "restart", "openclaw-gateway", "rmp-api",
                                                               "rmp-worker")]


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
    assert [c for c in host.calls if c[0] == "systemctl"] == [("systemctl", "restart", "rmp-api", "rmp-worker")]


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
    restarts = [c for c in host.calls if c[:2] == ("systemctl", "restart")]
    assert restarts == [("systemctl", "restart", "openclaw-gateway", "rmp-api", "rmp-worker")] * 2


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
    assert [c for c in host.calls if c[:2] == ("systemctl", "restart")] == [("systemctl", "restart", "rmp-api", "rmp-worker")]

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
    """systemd-run and systemctl's unit list as the launcher calls them: each start's argv is recorded, or raises while
    ``fail`` is set, and ``units`` are the deploy units listed. Every other command runs for real."""
    real, state = subprocess.run, SimpleNamespace(argvs=[], fail=False, units=[])

    def run(argv, **kwargs):
        if argv[:2] == ["systemctl", "list-units"]:
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout="".join(f"{unit} loaded active running Deploy\n" for unit in state.units))
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


def test_the_deploy_unit_holds_the_lock_records_the_result_and_starts_the_reply_run(tmp_path, monkeypatch):
    module_spec = importlib.util.spec_from_file_location("coding_deploy", ROOT / "ops" / "coding_deploy.py")
    unit = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(unit)
    lock = tmp_path / "code-reload.lock"
    monkeypatch.setattr(unit.deploy, "CODE_RELOAD_LOCK", lock)
    monkeypatch.setattr(unit.deploy, "RUNS_DIR", tmp_path / "runs")
    held = []

    def self_deploy(the_spec, host, *, log):
        with open(lock, "w") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
        return {"status": "deployed", "summary": "Deployed cccccccccccc to main."}

    monkeypatch.setattr(unit.deploy, "self_deploy", self_deploy)
    added, recorded = [], []

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
    context = {"task_id": "t1", "session_key": "s", "intent": "Fix it", "task_type": "user", "tags": []}
    deploy.queue({"task_id": "t1", "old": "b" * 40, "head": "c" * 40, "context": context,
                  "report": {"brief": {}, "review": {}, "evidence": {}}})
    path = deploy.spec_file("t1")

    assert unit.main(str(path)) == 0 and held == [True]
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
    assert [c for c in host.calls if c[0] == "systemctl"] == [("systemctl", "restart", "openclaw-gateway")]


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
    assert deploy.gate(host) == "1 active user task(s)"
    assert deploy.watch_main(host) is None and systemd_run.argvs == []
    assert json.loads(deploy.spec_file(deploy.MAIN).read_text()) == main_spec(head)
    assert json.loads(deploy.state_file(deploy.MAIN).read_text())["phase"] == "queued"

    host.held = True
    assert deploy.gate(host) == "the code-reload lock is held"
    assert deploy.watch_main(host) is None and systemd_run.argvs == []
    host.held, host.units = False, ["aura-deploy-t9.service"]
    assert deploy.gate(host) == "a deploy unit is running"
    assert deploy.watch_main(host) is None and systemd_run.argvs == []
    host.units = []
    assert deploy.watch_main(host) == "aura-deploy-main" and len(systemd_run.argvs) == 1
    assert systemd_run.argvs[0][1] == "--unit=aura-deploy-main"
    assert systemd_run.argvs[0][-1] == str(deploy.spec_file(deploy.MAIN))
    assert json.loads(deploy.state_file(deploy.MAIN).read_text())["launches"] == 1


def test_an_idle_count_that_fails_keeps_the_deploy_queued(repos, github_ci, systemd_run):
    merged_on_github(repos, {"app/a.py": "A = 2\n"})
    host = FakeHost(repos.live, busy=[RuntimeError("could not connect to server"), RuntimeError("timeout")])
    assert deploy.gate(host) == "the idle count failed: could not connect to server"
    assert deploy.watch_main(host) is None and systemd_run.argvs == []
    assert json.loads(deploy.state_file(deploy.MAIN).read_text())["phase"] == "queued"
    assert deploy.watch_main(host) == "aura-deploy-main" and len(systemd_run.argvs) == 1


def test_a_reviewed_run_launches_before_githubs_main(repos, systemd_run):
    deploy.queue(main_spec("a" * 40))
    deploy.queue(reviewed(repos, "t2"))
    deploy.queue(reviewed(repos, "t1"))
    host = FakeHost(repos.live)
    assert deploy.next_deploy(host) == "aura-deploy-t2"
    host.units = ["aura-deploy-t2.service"]
    assert deploy.next_deploy(host) is None
    host.units = []
    deploy.state_file("t2").unlink()
    assert deploy.next_deploy(host) == "aura-deploy-t1"
    deploy.state_file("t1").unlink()
    assert deploy.next_deploy(host) == "aura-deploy-main"
    assert [argv[1] for argv in systemd_run.argvs] == [
        "--unit=aura-deploy-t2", "--unit=aura-deploy-t1", "--unit=aura-deploy-main"]


def test_a_run_queued_for_two_hours_is_postponed(repos, github_ci, systemd_run, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    lock = repos.tmp / "code-reload.lock"
    monkeypatch.setattr(deploy, "CODE_RELOAD_LOCK", lock)
    monkeypatch.setattr(deploy, "self_deploy", lambda *args, **kwargs: pytest.fail("a postponed run deployed"))
    busy = FakeHost(repos.live, busy=[1] * 10)

    deploy.queue(reviewed(repos))
    queued_for("t1", deploy.IDLE_WAIT_SEC - 60)
    assert deploy.next_deploy(busy) is None and systemd_run.argvs == []
    queued_for("t1", 60)
    assert deploy.next_deploy(busy) == "aura-deploy-t1"
    assert systemd_run.argvs[-1][-2:] == [str(deploy.spec_file("t1")), "--postpone"]
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert unit.main(str(deploy.spec_file("t1")), postpone=True) == 1
    result = json.loads(deploy.spec_file("t1").with_name("result.json").read_text())
    assert result["status"] == "postponed" and result["head"] == "c" * 40
    assert result["summary"] == "Aura stayed busy for 2 hours, so nothing was deployed; main is unchanged."
    assert not deploy.state_file("t1").exists() and seen.notes == []
    name, payload = seen.client.start_workflow.await_args.args
    assert name == "CodingTaskWorkflow" and payload["report"]["shipped"]["status"] == "postponed"

    deploy.queue(reviewed(repos, "t2"))
    deploy.write_json(deploy.state_file("t2"), {**json.loads(deploy.state_file("t2").read_text()), "phase": "landed"})
    queued_for("t2", deploy.IDLE_WAIT_SEC)
    assert deploy.next_deploy(busy) == "aura-deploy-t2"
    assert unit.main(str(deploy.spec_file("t2")), postpone=True) == 1
    assert json.loads(deploy.spec_file("t2").with_name("result.json").read_text())["summary"] == (
        "Aura stayed busy for 2 hours, so nothing was deployed; GitHub's main has the change, and it goes live with "
        "GitHub's main once Aura is idle.")

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


def test_a_unit_that_cannot_take_the_lock_leaves_its_run_queued(repos, monkeypatch):
    unit, seen = deploy_unit(monkeypatch)
    lock = repos.tmp / "code-reload.lock"
    monkeypatch.setattr(deploy, "CODE_RELOAD_LOCK", lock)
    monkeypatch.setattr(deploy, "LOCK_WAIT_SEC", 1)
    monkeypatch.setattr(deploy, "self_deploy", lambda *args, **kwargs: pytest.fail("deployed without the lock"))
    deploy.queue(reviewed(repos))
    queued = deploy.state_file("t1").read_text()
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert unit.main(str(deploy.spec_file("t1"))) == 0
    assert deploy.state_file("t1").read_text() == queued
    assert not deploy.spec_file("t1").with_name("result.json").exists()
    assert seen.added == [] and seen.notes == [] and seen.client.start_workflow.await_count == 0


def test_a_unit_that_finds_aura_busy_leaves_its_run_queued(repos, monkeypatch):
    head = repos.change({"app/a.py": "A = 2\n"})
    for count in (1, RuntimeError("could not connect to server")):
        host = FakeHost(repos.live, busy=[count])
        assert deploy.self_deploy(spec(repos, head), host, log=lambda m: None) is None
        assert host.calls == [] and git(repos.live, "rev-parse", "main") == repos.old

    unit, seen = deploy_unit(monkeypatch)
    monkeypatch.setattr(deploy, "CODE_RELOAD_LOCK", repos.tmp / "code-reload.lock")
    monkeypatch.setattr(deploy, "Host", lambda: FakeHost(repos.live, busy=[1]))
    deploy.queue(reviewed(repos, head=head))
    queued = deploy.state_file("t1").read_text()
    assert unit.main(str(deploy.spec_file("t1"))) == 0
    assert deploy.state_file("t1").read_text() == queued
    assert not deploy.spec_file("t1").with_name("result.json").exists() and seen.added == []
    assert git(repos.live, "rev-parse", "main") == repos.old


def test_a_held_launch_lock_launches_nothing(repos, systemd_run):
    deploy.queue(reviewed(repos))
    with open(deploy.RUNS_DIR / ".launch.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert deploy.next_deploy(FakeHost(repos.live)) is None
    assert systemd_run.argvs == []
    assert deploy.next_deploy(FakeHost(repos.live)) == "aura-deploy-t1" and len(systemd_run.argvs) == 1
