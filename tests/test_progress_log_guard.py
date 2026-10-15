"""The CI guard for the append-only progress logs (.github/scripts/progress_log_guard.py) against real git repositories.

Each test builds a repository with two progress logs, docs/PLATFORM_HARDENING_PROGRESS.md and
docs/history/VISION_COMPLETION_PROGRESS.md, and two files outside the guard's globs, writes a GitHub event file and runs
the script as CI does, in the checkout.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "progress_log_guard.py"
LOG = "docs/PLATFORM_HARDENING_PROGRESS.md"
HISTORY_LOG = "docs/history/VISION_COMPLETION_PROGRESS.md"
OTHER = "docs/OTHER.md"
NESTED_LOG = "docs/runbooks/RESTORE_PROGRESS.md"

# Line 5 of the log is "Merged as abc1234.", and line 3 of the history log "## Step 1".
BASE_LOG = ("# Platform hardening progress\n\n## 2026-10-01 · batch 1\n\nMerged as abc1234.\n\n"
            "## 2026-10-02 · batch 1 deployed\n\nDeployed.\n")
BASE_HISTORY_LOG = "# Vision completion progress\n\n## Step 1\n\nDone.\n"


def entry(title: str) -> str:
    return f"\n## 2026-10-14 · {title}\n\nAppended.\n"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict, message: str) -> str:
    """Writes each file, or deletes it where its text is None, and commits."""
    for path, text in files.items():
        if text is None:
            (repo / path).unlink()
            continue
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    git(tmp_path, "init", "-q", "-b", "main", str(path))
    base = commit(path, {LOG: BASE_LOG, HISTORY_LOG: BASE_HISTORY_LOG, OTHER: "other\n",
                         NESTED_LOG: "# Restore progress\n\nStarted.\n", "app/x.py": "X = 1\n"}, "base")
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


def refused(result, *reasons):
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.startswith("Refused: "), result.stdout
    for reason in reasons:
        assert f"  {reason}\n" in result.stdout, result.stdout
    return result.stdout


def passed(result, reason):
    assert result.returncode == 0, result.stdout + result.stderr
    assert reason in result.stdout, result.stdout
    return result.stdout


CHECKED = f"Progress logs unchanged or appended to since the merge base: {LOG}, {HISTORY_LOG}"


def test_a_push_event_passes(repo):
    head = commit(repo.path, {LOG: "# Rewritten\n"}, "a rewrite pushed to main")
    passed(guard(repo, head=head, event="push"), "push event: the progress-log guard checks pull requests only")


def test_a_pull_request_without_a_progress_log_change_passes(repo):
    head = commit(repo.path, {"app/x.py": "X = 2\n"}, "code only")
    passed(guard(repo, head=head), CHECKED)


def test_an_append_passes(repo):
    head = commit(repo.path, {LOG: BASE_LOG + entry("batch 2"), HISTORY_LOG: BASE_HISTORY_LOG + entry("step 2")},
                  "new entries at the end")
    passed(guard(repo, head=head), CHECKED)


def test_a_new_progress_log_passes(repo):
    head = commit(repo.path, {"docs/NEW_PROGRESS.md": "# New\n", "docs/history/OLD_PROGRESS.md": "# Old\n"},
                  "two new logs")
    passed(guard(repo, head=head), CHECKED)


def test_an_edited_line_is_refused_and_named(repo):
    head = commit(repo.path, {LOG: BASE_LOG.replace("abc1234", "def5678") + entry("batch 2")}, "a corrected sha")
    # Another label does not let the rewrite through.
    message = refused(guard(repo, head=head, labels=["documentation"]),
                      f"{LOG}: line 5 is the first line that differs from the merge base")
    assert HISTORY_LOG not in message
    assert "keep every existing line as it is and append new entries at the end of the file" in message
    assert "needs the progress-log-rewrite label on the pull request" in message


def test_a_removed_line_is_refused(repo):
    head = commit(repo.path, {HISTORY_LOG: BASE_HISTORY_LOG.replace("## Step 1\n\n", "")}, "a heading dropped")
    refused(guard(repo, head=head), f"{HISTORY_LOG}: line 3 is the first line that differs from the merge base")


def test_an_entry_put_before_the_old_ones_is_refused_and_each_rewritten_log_is_named(repo):
    # The log keeps every old line, but after the new entry rather than as its start.
    head = commit(repo.path, {LOG: "## 2026-10-14 · batch 2\n\nPut first.\n\n" + BASE_LOG,
                              HISTORY_LOG: BASE_HISTORY_LOG.replace("Done.", "Done twice.")}, "newest first")
    refused(guard(repo, head=head), f"{LOG}: line 1 is the first line that differs from the merge base",
            f"{HISTORY_LOG}: line 5 is the first line that differs from the merge base")


def test_a_deleted_progress_log_is_refused(repo):
    head = commit(repo.path, {LOG: None}, "the log goes")
    refused(guard(repo, head=head), f"{LOG}: no longer exists at this path (deleted or renamed)")


def test_a_renamed_progress_log_is_refused(repo):
    head = commit(repo.path, {LOG: None, "docs/history/PLATFORM_HARDENING_PROGRESS.md": BASE_LOG}, "the log moves")
    assert git(repo.path, "diff", "--name-status", "-M", repo.base, head).startswith("R100\t"), "git sees a rename"
    message = refused(guard(repo, head=head), f"{LOG}: no longer exists at this path (deleted or renamed)")
    assert "docs/history/PLATFORM_HARDENING_PROGRESS.md" not in message


def test_the_label_passes_a_rewrite(repo):
    head = commit(repo.path, {LOG: BASE_LOG.replace("abc1234", "[redacted]")}, "a redaction")
    message = passed(guard(repo, head=head, labels=["documentation", "progress-log-rewrite"]),
                     "Passes: the pull request carries the progress-log-rewrite label")
    assert f"  {LOG}: line 5 is the first line that differs from the merge base\n" in message


def test_entries_main_appended_after_the_branch_do_not_fail_the_branch_s_append(repo):
    git(repo.path, "checkout", "-q", "-b", "aura/change")
    head = commit(repo.path, {LOG: BASE_LOG + entry("the branch's entry")}, "the branch appends")
    git(repo.path, "checkout", "-q", "main")
    # Compared with main rather than the merge base, the branch's log would lack main's entry and be refused.
    main = commit(repo.path, {LOG: BASE_LOG + entry("main's entry")}, "main appends after the branch point")
    passed(guard(repo, base=main, head=head), CHECKED)


def test_files_outside_the_two_globs_are_not_checked(repo):
    head = commit(repo.path, {OTHER: "rewritten\n", NESTED_LOG: "# Rewritten\n"}, "rewrites outside the globs")
    message = passed(guard(repo, head=head), CHECKED)
    assert OTHER not in message and NESTED_LOG not in message


def test_a_merge_base_without_progress_logs_passes(repo):
    git(repo.path, "checkout", "-q", "--orphan", "bare")
    git(repo.path, "rm", "-rqf", "docs")
    head = commit(repo.path, {"app/x.py": "X = 2\n"}, "no progress logs")
    passed(guard(repo, base=head, head=head), "No progress logs at the merge base\n")


def test_a_revert_of_an_append_is_refused_and_names_the_rollback_case(repo):
    appended = commit(repo.path, {LOG: BASE_LOG + entry("batch 2"), "app/x.py": "X = 2\n"}, "a change and its entry")
    git(repo.path, "-c", "user.name=t", "-c", "user.email=t@t", "revert", "--no-edit", appended)
    revert = git(repo.path, "rev-parse", "HEAD")
    message = refused(guard(repo, base=appended, head=revert),
                      f"{LOG}: line 10 is the first line that differs from the merge base")
    assert "RMP's automatic rollback pull request (aura/revert-<sha>), is refused like any other rewrite" in message
    assert ("a reviewer adds the progress-log-rewrite label and merges it with a merge commit (aura-github gh pr "
            "merge <n> --merge), not a squash") in message


def test_a_git_error_fails_closed(repo):
    # A head the checkout lacks, as in a shallow clone, stops the step rather than passing it.
    result = guard(repo, head="0" * 40)
    assert result.returncode == 1 and "CalledProcessError" in result.stderr, result.stdout + result.stderr
