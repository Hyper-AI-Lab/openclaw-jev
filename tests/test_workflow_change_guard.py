"""The CI guard for workflow changes (.github/scripts/workflow_change_guard.py) against real git repositories.

Each test builds a repository with a workflow, app/workflows/w.py, and its replay fixture,
tests/fixtures/w_histories.json, writes a GitHub event file and runs the script as CI does, in the checkout.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "workflow_change_guard.py"
W = "app/workflows/w.py"
FIXTURE = "tests/fixtures/w_histories.json"


def workflow(*lines: str) -> str:
    return "from temporalio import workflow\n\n\nasync def run():\n" + "".join(f"    {line}\n" for line in lines)


def histories(*keys: str, recording: int = 1) -> str:
    return json.dumps({key: {"events": [recording]} for key in keys})


# At the base, w.py has one patch, "old", and its fixture pins both paths.
BASE_W = workflow('if workflow.patched("old"):', '    return "b"', 'return "a"')
CHANGED_W = workflow('if workflow.patched("old"):', '    return "b"', 'return "c"')
PATCHED_W = workflow('if workflow.patched("old"):', '    return "b"', 'if workflow.patched("x"):', '    return "c"',
                     'return "a"')


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict, message: str) -> str:
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    git(tmp_path, "init", "-q", "-b", "main", str(path))
    base = commit(path, {W: BASE_W, FIXTURE: histories("old-path", "new-path"), "docs/d.md": "docs\n"}, "base")
    return SimpleNamespace(path=path, base=base, events=tmp_path / "event.json")


def guard(repo, *, head, base=None, labels=(), event="pull_request"):
    if event == "pull_request":
        payload = {"pull_request": {"base": {"sha": base or repo.base}, "head": {"sha": head},
                                    "labels": [{"name": name} for name in labels]}}
    else:
        payload = {"ref": "refs/heads/main", "after": head}
    repo.events.write_text(json.dumps(payload))
    env = {**os.environ, "GITHUB_EVENT_NAME": event, "GITHUB_EVENT_PATH": str(repo.events)}
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=repo.path, env=env, capture_output=True, text=True)


def refused(result, *changed):
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.startswith("Refused: "), result.stdout
    for path in changed:
        assert f"  {path}\n" in result.stdout
    return result.stdout


def passed(result, reason):
    assert result.returncode == 0, result.stdout + result.stderr
    assert reason in result.stdout, result.stdout


def test_a_push_event_passes(repo):
    head = commit(repo.path, {W: CHANGED_W}, "a workflow change pushed to main")
    passed(guard(repo, head=head, event="push"), "push event: the workflow-change guard checks pull requests only")


def test_a_pull_request_without_a_workflow_change_passes(repo):
    head = commit(repo.path, {"docs/d.md": "more docs\n"}, "docs")
    passed(guard(repo, head=head), "no change under app/workflows")


def test_a_workflow_change_alone_is_refused(repo):
    head = commit(repo.path, {W: CHANGED_W}, "a command-order change")
    message = refused(guard(repo, head=head), W)
    assert 'a new workflow.patched("<id>"), or workflow.deprecate_patch("<id>")' in message
    assert "a new key in tests/fixtures/*_histories*.json" in message
    assert "the no-replay-impact label" in message
    assert "deprecating or removing a patch updates the fixtures that pin it in the same change" in message


def test_a_new_patch_id_passes(repo):
    head = commit(repo.path, {W: PATCHED_W}, "a command-order change behind a patch")
    passed(guard(repo, head=head), 'Passes: new patch ids patched("x")')


def test_deprecating_a_patch_of_the_base_passes(repo):
    head = commit(repo.path, {W: workflow('workflow.deprecate_patch("old")', 'return "b"')}, "the old branch goes")
    passed(guard(repo, head=head), 'Passes: new patch ids deprecate_patch("old")')


def test_a_new_fixture_key_passes(repo):
    head = commit(repo.path, {W: CHANGED_W, FIXTURE: histories("old-path", "new-path", "c-path")}, "a recorded change")
    passed(guard(repo, head=head), "Passes: new fixture keys w_histories.json: c-path")


def test_only_a_removed_fixture_key_is_refused(repo):
    head = commit(repo.path, {W: CHANGED_W, FIXTURE: histories("new-path")}, "a fixture key dropped")
    refused(guard(repo, head=head), W)


def test_a_re_recorded_fixture_key_is_refused(repo):
    head = commit(repo.path, {W: CHANGED_W, FIXTURE: histories("old-path", "new-path", recording=2)}, "re-recorded")
    refused(guard(repo, head=head), W)


def test_a_patch_id_moved_to_another_file_is_refused(repo):
    moved = {W: workflow('return "a"'), "app/workflows/v.py": workflow('if workflow.patched("old"):', '    return "b"')}
    head = commit(repo.path, moved, "the patch moves to v.py")
    refused(guard(repo, head=head), "app/workflows/v.py", W)


def test_the_label_passes(repo):
    head = commit(repo.path, {W: CHANGED_W}, "a reviewed change with no command-order effect")
    passed(guard(repo, head=head, labels=["documentation", "no-replay-impact"]),
           "Passes: the pull request carries the no-replay-impact label")


def test_a_patch_id_main_gained_after_the_branch_does_not_let_the_branch_through(repo):
    git(repo.path, "checkout", "-q", "-b", "aura/change")
    head = commit(repo.path, {W: CHANGED_W}, "the branch's command-order change")
    git(repo.path, "checkout", "-q", "main")
    main = commit(repo.path, {"app/workflows/u.py": workflow('if workflow.patched("main-only"):', '    return "m"')},
                  "main gains a patch of its own")
    message = refused(guard(repo, base=main, head=head), W)
    assert "u.py" not in message, "the comparison starts at the merge base, not at main"


def test_a_pure_revert_of_a_patch_and_its_fixture_key_is_refused_and_names_the_rollback_case(repo):
    added = commit(repo.path, {W: PATCHED_W, FIXTURE: histories("old-path", "new-path", "x-path")},
                   "a patch and its fixture")
    git(repo.path, "-c", "user.name=t", "-c", "user.email=t@t", "revert", "--no-edit", added)
    revert = git(repo.path, "rev-parse", "HEAD")
    message = refused(guard(repo, base=added, head=revert), W)
    assert "RMP's automatic rollback pull request (aura/revert-<sha>), is refused like any other change" in message
    assert "a reviewer adds the no-replay-impact label and merges it with a merge commit" in message
