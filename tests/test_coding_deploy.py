"""Shipping approved changes against hermetic repos: the live repo, its GitHub remote, the review repository, mirrors.

git runs for real; the host's systemctl, pip, checks and waiting are a recording fake.
"""
import fcntl
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.coding import deploy, verify, workspace

ROOT = Path(__file__).resolve().parents[1]
BASE = {"app/a.py": "A = 1\n", "worker.py": "W = 1\n", "requirements.txt": "x==1\n", "plugins/p/index.js": "v1\n",
        "plugins/p/old.js": "gone soon\n", "web-stack/backends/w.py": "W = 1\n", "systemd/aura-x.service": "[Service]\n",
        ".cursor/rules/r.mdc": "rule v1\n", "docs/d.md": "docs\n"}


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
    for prefix in ("plugins/", "web-stack/", "systemd/", ".cursor/rules/"):
        target = tmp_path / "mirror" / prefix.strip("/").replace("/", "_")
        for path, text in BASE.items():
            if path.startswith(prefix):
                write(target, {path[len(prefix):]: text})
        mirrors.append((prefix, target))
    monkeypatch.setattr(deploy, "MIRRORS", tuple(mirrors))

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
    def __init__(self, live, *, busy=(), checks=()):
        super().__init__(live)
        self.calls, self.busy, self.results = [], list(busy), list(checks)

    def active_user_tasks(self):
        return self.busy.pop(0) if self.busy else 0

    def systemctl(self, *args):
        self.calls.append(("systemctl", *args))

    def pip_install(self):
        self.calls.append(("pip",))

    def push(self):
        git(self.live, "push", "-q", "origin", "main")
        self.calls.append(("push", git(self.live, "rev-parse", "main")))

    def readiness_baseline(self):
        return ["telemetry"]

    def checks(self, restarted, baseline):
        self.calls.append(("checks", tuple(restarted), tuple(baseline)))
        return self.results.pop(0) if self.results else {"failure": None, "canary": "ok"}

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))


def spec(repos, head):
    return {"task_id": "t1", "old": repos.old, "head": head}


def test_a_self_deploy_fast_forwards_pushes_syncs_and_restarts_only_what_changed(repos):
    head = repos.change({"app/a.py": "A = 2\n", "plugins/p/index.js": "v2\n", "web-stack/backends/w.py": "W = 2\n",
                         ".cursor/rules/r.mdc": "rule v2\n", "docs/d.md": "more docs\n"}, deletes=["plugins/p/old.js"])
    host = FakeHost(repos.live)
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)

    assert result["status"] == "deployed" and result["pushed"] and "health and readiness passed and the canary passed" in result["summary"]
    assert git(repos.live, "rev-parse", "main") == head == git(repos.origin, "rev-parse", "main")
    plugins, web, rules = repos.mirror["plugins/"], repos.mirror["web-stack/"], repos.mirror[".cursor/rules/"]
    assert (plugins / "p" / "index.js").read_text() == "v2\n" and not (plugins / "p" / "old.js").exists()
    assert (web / "backends" / "w.py").read_text() == "W = 2\n" and (rules / "r.mdc").read_text() == "rule v2\n"
    services = ("aura-web-backends", "openclaw-gateway", "rmp-api", "rmp-worker")
    assert [c for c in host.calls if c[0] in ("systemctl", "pip")] == [("systemctl", "restart", *services)]
    assert ("checks", services, ("telemetry",)) in host.calls


def test_requirements_and_units_are_installed_before_the_restart(repos):
    head = repos.change({"requirements.txt": "x==2\n", "systemd/aura-x.service": "[Service]\nNice=5\n"})
    host = FakeHost(repos.live)
    assert deploy.self_deploy(spec(repos, head), host, log=lambda m: None)["status"] == "deployed"
    assert [c for c in host.calls if c[0] in ("systemctl", "pip")] == [
        ("pip",), ("systemctl", "daemon-reload"), ("systemctl", "restart", "aura-x", "rmp-api", "rmp-worker")]
    assert (repos.mirror["systemd/"] / "aura-x.service").read_text() == "[Service]\nNice=5\n"


def test_a_hand_edited_mirror_blocks_the_deploy_before_main_moves(repos):
    (repos.mirror["plugins/"] / "p" / "index.js").write_text("patched by hand\n")
    head = repos.change({"plugins/p/index.js": "v2\n"})
    host = FakeHost(repos.live)
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)

    assert result["status"] == "blocked" and "plugins/p/index.js" in result["summary"]
    assert git(repos.live, "rev-parse", "main") == repos.old == git(repos.origin, "rev-parse", "main")
    assert not [c for c in host.calls if c[0] == "systemctl"]


def test_a_mirror_that_is_only_behind_is_updated(repos):
    write(repos.live, {"web-stack/backends/w.py": "W = 2\n"})
    repos.old = commit(repos.live, "a change the mirror never got")
    head = repos.change({"web-stack/backends/w.py": "W = 3\n"})
    result = deploy.self_deploy(spec(repos, head), FakeHost(repos.live), log=lambda m: None)
    assert result["status"] == "deployed" and (repos.mirror["web-stack/"] / "backends" / "w.py").read_text() == "W = 3\n"


def test_failed_checks_revert_main_restore_the_mirrors_restart_and_push_the_revert(repos):
    head = repos.change({"app/a.py": "A = 2\n", "plugins/p/index.js": "v2\n"}, deletes=["plugins/p/old.js"])
    host = FakeHost(repos.live, checks=[{"failure": "CANARY FAIL: task failed", "canary": None}, {"failure": None, "canary": "ok"}])
    result = deploy.self_deploy(spec(repos, head), host, log=lambda m: None)

    revert = git(repos.live, "rev-parse", "main")
    assert result["status"] == "rolled_back" and result["revert"] == revert and "Aura is healthy again" in result["summary"]
    assert git(repos.live, "rev-parse", f"{revert}^{{tree}}") == git(repos.live, "rev-parse", f"{repos.old}^{{tree}}")
    assert git(repos.origin, "rev-parse", "main") == revert
    plugins = repos.mirror["plugins/"]
    assert (plugins / "p" / "index.js").read_text() == "v1\n" and (plugins / "p" / "old.js").read_text() == "gone soon\n"
    restarts = [c for c in host.calls if c[:2] == ("systemctl", "restart")]
    assert restarts == [("systemctl", "restart", "openclaw-gateway", "rmp-api", "rmp-worker")] * 2


def test_the_deploy_waits_for_idle_and_postpones_when_aura_stays_busy(repos, monkeypatch):
    head = repos.change({"app/a.py": "A = 2\n"})
    host = FakeHost(repos.live, busy=[2, 1, 0])
    assert deploy.self_deploy(spec(repos, head), host, log=lambda m: None)["status"] == "deployed"
    assert [c for c in host.calls if c[0] == "sleep"] == [("sleep", deploy.IDLE_POLL_SEC)] * 2

    monkeypatch.setattr(deploy, "IDLE_WAIT_SEC", 30)
    git(repos.live, "reset", "-q", "--hard", repos.old)
    busy = FakeHost(repos.live, busy=[1] * 10)
    assert deploy.self_deploy(spec(repos, head), busy, log=lambda m: None)["status"] == "postponed"
    assert git(repos.live, "rev-parse", "main") == repos.old


def test_main_moving_while_the_deploy_waited_deploys_nothing(repos):
    head = repos.change({"app/a.py": "A = 2\n"})
    write(repos.live, {"docs/d.md": "another deploy\n"})
    moved = commit(repos.live, "another deploy")
    result = deploy.self_deploy(spec(repos, head), FakeHost(repos.live), log=lambda m: None)
    assert result["status"] == "failed" and "main moved" in result["summary"] and git(repos.live, "rev-parse", "main") == moved


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


def test_the_hand_off_starts_one_root_unit_and_forgets_a_failed_start(repos, monkeypatch):
    monkeypatch.setattr(deploy, "RUNS_DIR", repos.tmp / "runs")
    started = []

    def run(argv, **kwargs):
        started.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    the_spec = {"task_id": "t1", "old": repos.old, "head": "c" * 40}
    assert deploy.hand_off(the_spec, live=repos.live) == "aura-deploy-t1"
    assert deploy.hand_off(the_spec, live=repos.live) == "aura-deploy-t1" and len(started) == 1
    argv = started[0]
    assert argv[:2] == ["systemd-run", "--unit=aura-deploy-t1"] and not any(a.startswith("--uid") for a in argv)
    assert "--property=EnvironmentFile=-/etc/rmp/rmp.env" in argv and argv[-2:] == [
        str(repos.live / "ops" / "coding_deploy.py"), str(deploy.spec_file("t1"))]
    assert json.loads(deploy.spec_file("t1").read_text())["head"] == "c" * 40

    def broken(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(deploy.subprocess, "run", broken)
    with pytest.raises(subprocess.CalledProcessError):
        deploy.hand_off({**the_spec, "head": "d" * 40}, live=repos.live)
    assert not deploy.spec_file("t1").exists()


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
    added = []
    db = SimpleNamespace(add=added.append, get=AsyncMock(return_value=SimpleNamespace(supplementary_context={"a": 1})),
                         commit=AsyncMock())
    session = AsyncMock()
    session.__aenter__.return_value = db
    monkeypatch.setattr(unit, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(unit, "engine", SimpleNamespace(dispose=AsyncMock()))
    client = SimpleNamespace(start_workflow=AsyncMock())
    monkeypatch.setattr(unit, "connect_temporal", AsyncMock(return_value=client))
    path = tmp_path / "deploy.json"
    context = {"task_id": "t1", "session_key": "s", "intent": "Fix it", "task_type": "user", "tags": []}
    path.write_text(json.dumps({"task_id": "t1", "old": "b" * 40, "head": "c" * 40, "context": context,
                                "report": {"brief": {}, "review": {}, "evidence": {}}}))

    assert unit.main(str(path)) == 0 and held == [True]
    assert added[0].event_type == "coding.deploy" and added[0].event_payload["status"] == "deployed"
    name, payload = client.start_workflow.await_args.args
    assert name == "CodingTaskWorkflow" and client.start_workflow.await_args.kwargs["id"] == "workflow-t1"
    assert payload["task_id"] == "t1" and payload["report"]["shipped"]["status"] == "deployed"
    assert db.get.return_value.supplementary_context == {"a": 1, "coding_deploy": payload["report"]["shipped"]}
