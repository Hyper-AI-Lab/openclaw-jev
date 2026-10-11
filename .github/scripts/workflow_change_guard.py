#!/usr/bin/env python3
"""CI guard: a pull request that changes app/workflows carries a new patch id, a new replay fixture key or the
no-replay-impact label.

Temporal replays every open run against the code a worker runs, so a change in a workflow's command order breaks the
runs that started before it, unless the change sits behind a new ``workflow.patched`` id. The guard only forces that
decision; ``tests/test_workflow_replay.py`` is the real gate.

The pull request's base and head come from the event payload ($GITHUB_EVENT_PATH), and the comparison starts at their
merge base, so only the pull request's own commits are compared: not those that reached main after it branched.
Patch ids are read with ``ast`` and fixture keys with ``json`` at both revisions. A file that does not parse is not
caught: the step fails closed. Run by .github/workflows/ci.yml in a full checkout (fetch-depth 0). Standard library
only.
"""
import ast
import json
import os
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import PurePosixPath

LABEL = "no-replay-impact"
WORKFLOWS = "app/workflows"
FIXTURES = "tests/fixtures"
FIXTURE_FILES = "*_histories*.json"
PATCH_CALLS = ("patched", "deprecate_patch")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE, text=True).stdout


def tracked(rev: str, directory: str) -> list[str]:
    return git("ls-tree", "-r", "--name-only", rev, "--", directory).splitlines()


def patch_ids(rev: str) -> set[tuple[str, str]]:
    """(kind, id) of every ``patched``/``deprecate_patch`` call with a string-literal id in app/workflows at rev."""
    ids = set()
    for path in tracked(rev, WORKFLOWS):
        if not path.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(git("show", f"{rev}:{path}"), filename=f"{rev}:{path}")):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in PATCH_CALLS and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                ids.add((node.func.attr, node.args[0].value))
    return ids


def fixture_keys(rev: str) -> set[tuple[str, str]]:
    """(file name, top-level key) over tests/fixtures/*_histories*.json at rev."""
    keys = set()
    for path in tracked(rev, FIXTURES):
        file = PurePosixPath(path)
        if str(file.parent) == FIXTURES and fnmatch(file.name, FIXTURE_FILES):
            keys.update((file.name, key) for key in json.loads(git("show", f"{rev}:{path}")).keys())
    return keys


def refusal(changed: list[str]) -> str:
    return "\n".join([
        "Refused: this pull request changes workflow code without a new patch id, a new replay fixture key or the "
        f"{LABEL} label.",
        "Changed under app/workflows:",
        *(f"  {path}" for path in changed),
        "",
        "Temporal replays every open run against the new code, so a change in command order breaks the runs that "
        "started before it. One of these lets the change through:",
        '  1. a new patch id: a new workflow.patched("<id>"), or workflow.deprecate_patch("<id>") for a patched id;',
        "  2. a new key in tests/fixtures/*_histories*.json (re-recording or removing a key does not count);",
        f"  3. the {LABEL} label, for a reviewed change with no command-order effect. Adding it starts a new run of "
        "this check.",
        "",
        'The lifecycle: a command-order change goes behind a new workflow.patched("<id>") with a fixture of the new '
        "path, and deprecating or removing a patch updates the fixtures that pin it in the same change.",
        "",
        "Rollback reverts: a revert of a workflow change, including RMP's automatic rollback pull request "
        "(aura/revert-<sha>), is refused like any other change. Once the live checkout already runs the revert, a "
        f"reviewer adds the {LABEL} label and merges it with a merge commit.",
    ])


def main() -> int:
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event != "pull_request":
        print(f"{event or 'no'} event: the workflow-change guard checks pull requests only")
        return 0
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        pr = json.load(fh)["pull_request"]
    head = pr["head"]["sha"]
    merge_base = git("merge-base", pr["base"]["sha"], head).strip()
    changed = git("diff", "--name-only", merge_base, head, "--", WORKFLOWS).splitlines()
    if not changed:
        print("no change under app/workflows")
        return 0
    new_ids = sorted(patch_ids(head) - patch_ids(merge_base))
    new_keys = sorted(fixture_keys(head) - fixture_keys(merge_base))
    labelled = LABEL in {label["name"] for label in pr["labels"]}
    if not (new_ids or new_keys or labelled):
        print(refusal(changed))
        return 1
    print("Changed under app/workflows: " + ", ".join(changed))
    if new_ids:
        print("Passes: new patch ids " + ", ".join(f'{kind}("{id_}")' for kind, id_ in new_ids))
    if new_keys:
        print("Passes: new fixture keys " + ", ".join(f"{file}: {key}" for file, key in new_keys))
    if labelled:
        print(f"Passes: the pull request carries the {LABEL} label")
    return 0


if __name__ == "__main__":
    sys.exit(main())
