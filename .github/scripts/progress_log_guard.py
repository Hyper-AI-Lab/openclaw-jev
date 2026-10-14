#!/usr/bin/env python3
"""CI guard: a pull request only appends to the progress logs, or carries the progress-log-rewrite label.

docs/*_PROGRESS.md and docs/history/*_PROGRESS.md are append-only: they keep the record of what was done and decided,
in order. Every such log at the merge base must still exist at the head under the same path, and its bytes there must
start with its bytes at the merge base. A deleted, renamed, edited or removed line is refused, naming the file and the
first line that differs; a new log and an entry appended at the end pass.

The pull request's base and head come from the event payload ($GITHUB_EVENT_PATH), and the comparison starts at their
merge base, so entries that reached main after the pull request branched do not count against it. Git errors are not
caught: the step fails closed. Run by .github/workflows/ci.yml in a full checkout (fetch-depth 0). Standard library
only.
"""
import json
import os
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import PurePosixPath

LABEL = "progress-log-rewrite"
DOCS = "docs"
LOG_DIRS = ("docs", "docs/history")
LOG_FILES = "*_PROGRESS.md"


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE).stdout


def logs(rev: str) -> list[str]:
    """The progress logs at rev: docs/*_PROGRESS.md and docs/history/*_PROGRESS.md."""
    paths = git("ls-tree", "-r", "-z", "--name-only", rev, "--", DOCS).decode().split("\0")
    return [path for path in paths
            if str(PurePosixPath(path).parent) in LOG_DIRS and fnmatch(PurePosixPath(path).name, LOG_FILES)]


def first_differing_line(old: bytes, new: bytes) -> int:
    """The number of the first line of old that new does not keep byte for byte."""
    same = next((i for i, (a, b) in enumerate(zip(old, new)) if a != b), min(len(old), len(new)))
    return old.count(b"\n", 0, same) + 1


def rewrites(merge_base: str, head: str, checked: list[str]) -> list[str]:
    """One "<path>: <what happened>" line for each log of the merge base that head does not keep as a byte prefix."""
    at_head = set(logs(head))
    found = []
    for path in checked:
        if path not in at_head:
            found.append(f"{path}: no longer exists at this path (deleted or renamed)")
            continue
        old, new = (git("cat-file", "blob", f"{rev}:{path}") for rev in (merge_base, head))
        if not new.startswith(old):
            found.append(f"{path}: line {first_differing_line(old, new)} is the first line that differs from the "
                         "merge base")
    return found


def refusal(found: list[str]) -> str:
    return "\n".join([
        "Refused: this pull request rewrites an append-only progress log.",
        *(f"  {line}" for line in found),
        "",
        "docs/*_PROGRESS.md and docs/history/*_PROGRESS.md are append-only: keep every existing line as it is and "
        "append new entries at the end of the file. A new progress log is fine.",
        f"A deliberate rewrite, such as a redaction, needs the {LABEL} label on the pull request. Adding it starts a "
        "new run of this check.",
        "",
        "Rollback reverts: a revert of commits that appended progress entries, including RMP's automatic rollback "
        "pull request (aura/revert-<sha>), is refused like any other rewrite. Once the live checkout already runs the "
        f"revert, a reviewer adds the {LABEL} label and merges it with a merge commit (aura-github gh pr merge <n> "
        "--merge), not a squash: until GitHub's main contains the live revert, nothing deploys.",
    ])


def main() -> int:
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event != "pull_request":
        print(f"{event or 'no'} event: the progress-log guard checks pull requests only")
        return 0
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        pr = json.load(fh)["pull_request"]
    head = pr["head"]["sha"]
    merge_base = git("merge-base", pr["base"]["sha"], head).decode().strip()
    checked = logs(merge_base)
    found = rewrites(merge_base, head, checked)
    if not found:
        print("Progress logs unchanged or appended to since the merge base: " + ", ".join(checked))
        return 0
    if LABEL not in {label["name"] for label in pr["labels"]}:
        print(refusal(found))
        return 1
    print("Rewritten progress logs:")
    print("\n".join(f"  {line}" for line in found))
    print(f"Passes: the pull request carries the {LABEL} label")
    return 0


if __name__ == "__main__":
    sys.exit(main())
