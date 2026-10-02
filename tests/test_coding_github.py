"""GitHub through gh with the token per command, protected main, and Kirill's note after a deploy."""
import json
import subprocess

import pytest

from app.coding import deploy, github, workspace

REPO = "Hyper-AI-Lab/openclaw-jev"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_gh_gets_the_token_in_its_environment_never_in_its_arguments(tmp_path, monkeypatch):
    token = tmp_path / "pat"
    token.write_text("tok-123\n")
    monkeypatch.setattr(workspace, "TOKEN_FILE", token)
    seen, gh = tmp_path / "seen.json", tmp_path / "gh"
    gh.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                  f"json.dump({{'argv': sys.argv[1:], 'token': os.environ.get('GH_TOKEN'), 'stdin': sys.stdin.read()}}, "
                  f"open({str(seen)!r}, 'w'))\nprint(json.dumps({{'sha': 'abc'}}))\n")
    gh.chmod(0o755)
    assert github.api("PUT", f"repos/{REPO}/pulls/3/merge", {"sha": "s", "merge_method": "squash"}, gh=str(gh)) == {"sha": "abc"}
    called = json.loads(seen.read_text())
    assert called["token"] == "tok-123" and "tok-123" not in " ".join(called["argv"])
    assert called["argv"] == ["api", "--method", "PUT", f"repos/{REPO}/pulls/3/merge", "--input", "-"]
    assert json.loads(called["stdin"]) == {"sha": "s", "merge_method": "squash"}
    gh.write_text("#!/bin/sh\necho 'HTTP 403: Resource not accessible by personal access token' >&2\nexit 1\n")
    with pytest.raises(github.GitHubError, match="403"):
        github.api("GET", f"repos/{REPO}", gh=str(gh))


@pytest.mark.parametrize("runs, state", [
    ([], "missing"),
    ([{"status": "in_progress", "started_at": "2"}], "pending"),
    ([{"status": "completed", "conclusion": "failure", "started_at": "1"},
      {"status": "completed", "conclusion": "success", "started_at": "2"}], "success"),
    ([{"status": "completed", "conclusion": "success", "started_at": "1"},
      {"status": "completed", "conclusion": "cancelled", "started_at": "2"}], "failure"),
])
def test_the_test_check_is_read_from_its_latest_run(monkeypatch, runs, state):
    seen = []
    monkeypatch.setattr(github, "api", lambda method, path, body=None: seen.append(path) or {"check_runs": runs})
    assert github.check("abc", REPO) == state
    assert seen == [f"repos/{REPO}/commits/abc/check-runs?check_name=test"]


def test_waiting_for_the_check_polls_until_it_finishes(monkeypatch):
    states = iter(["missing", "pending", "success"])
    monkeypatch.setattr(github, "check", lambda sha, repo: next(states))
    slept = []
    assert github.wait_check("abc", REPO, sleep=slept.append) == "success" and slept == [github.CHECK_POLL_SEC] * 2


def test_a_branch_with_an_open_pull_request_reuses_it_and_otherwise_opens_one(monkeypatch):
    calls = []

    def api(method, path, body=None):
        calls.append((method, path, body))
        return [{"number": 9, "html_url": "u9"}] if method == "GET" else {"number": 10, "html_url": "u10"}

    monkeypatch.setattr(github, "api", api)
    assert github.open_pull("aura/x", "T", "B", REPO) == {"number": 9, "url": "u9"}
    assert calls == [("GET", f"repos/{REPO}/pulls?state=open&head=Hyper-AI-Lab:aura/x", None)]
    monkeypatch.setattr(github, "api", lambda method, path, body=None: (
        calls.append((method, path, body)) or ([] if method == "GET" else {"number": 10, "html_url": "u10"})))
    assert github.open_pull("aura/x", "T", "B", REPO) == {"number": 10, "url": "u10"}
    assert calls[-1] == ("POST", f"repos/{REPO}/pulls", {"head": "aura/x", "base": "main", "title": "T", "body": "B"})


def test_land_pushes_the_branch_and_merges_once_the_check_passed_and_never_otherwise(tmp_path, monkeypatch):
    origin, live = tmp_path / "github.git", tmp_path / "live"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(live))
    (live / "a.txt").write_text("a\n")
    git(live, "add", "a.txt")
    git(live, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a")
    commit = git(live, "rev-parse", "HEAD")
    monkeypatch.setattr(workspace, "remote_url", lambda repo: str(origin))
    monkeypatch.setattr(github, "open_pull", lambda branch, title, body, repo: {"number": 5, "url": "u5"})
    checks, merged = iter(["success", "failure"]), []
    monkeypatch.setattr(github, "wait_check", lambda sha, repo, sleep=None: next(checks))
    monkeypatch.setattr(github, "merge", lambda number, sha, method, repo: merged.append((number, sha, method)) or "m" * 40)
    assert github.land(live, commit, "aura/x", "T", "B", repo=REPO, method="squash") == {
        "status": "merged", "pr": {"number": 5, "url": "u5"}, "merge": "m" * 40}
    assert git(origin, "rev-parse", "refs/heads/aura/x") == commit and merged == [(5, commit, "squash")]
    assert github.land(live, commit, "aura/x", "T", "B", repo=REPO, method="squash") == {
        "status": "blocked", "pr": {"number": 5, "url": "u5"}, "check": "failure"}
    assert len(merged) == 1


def test_main_is_protected_for_everyone_through_a_pull_request_and_the_test_check(monkeypatch):
    sent = []
    monkeypatch.setattr(github, "api", lambda method, path, body=None: sent.append((method, path, body)) or {})
    github.protect_main(REPO)
    [(method, path, body)] = sent
    assert (method, path) == ("PUT", f"repos/{REPO}/branches/main/protection")
    assert body["enforce_admins"] is True and body["required_status_checks"]["checks"] == [{"context": "test"}]
    assert body["required_pull_request_reviews"] == {"required_approving_review_count": 0}
    assert body["allow_force_pushes"] is False and body["allow_deletions"] is False


def test_kirills_note_links_each_pull_request():
    note = deploy.deploy_note({"status": "deployed", "commits": ["Fix the greeting (#12)", "Tidy the logs"],
                               "summary": "Deployed abc to main."}, REPO)
    assert note == ("Aura's change is live.\n• Fix the greeting (#12) https://github.com/Hyper-AI-Lab/openclaw-jev/pull/12\n"
                    "• Tidy the logs\nDeployed abc to main.")
