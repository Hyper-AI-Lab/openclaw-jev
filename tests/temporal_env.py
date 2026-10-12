"""Temporal for the tests: the test environments every Temporal test starts from, and the sandbox runner the
replays share.

The SDK runs the server binary it finds under a fixed name in the temp dir, and downloads it there when there is none.
``cached_binary`` hands the SDK a cached binary only when root or this user owns both the name and the file it points
to, and no group or other user can write that file. A cached binary that fails the rule fails the test instead,
because the SDK would run that same file. With none cached, the SDK downloads, as it does in CI.
"""
import importlib.metadata
import os
import stat
import tempfile
from typing import Any, Optional

from temporalio.testing import WorkflowEnvironment
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

# The client identity, which the workers reuse: recorded histories carry it instead of the recording host's pid@name.
IDENTITY = "rmp-tests"
VERSION = importlib.metadata.version("temporalio")
# The temp dir shared by every user, searched after this process's own.
SHARED_TMP = "/tmp"

WORKFLOW_RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules("app.orchestrator.web_capability"))
"""The default sandbox, with app.orchestrator.web_capability passed through.

Why: CatalogStepChildWorkflow builds its prompt with app/orchestrator/prompt_policy.py, whose
build_catalog_step_prompt imports obscura_available inside the function. That lazy import is a fresh sandbox import,
which asks this host's web stack over HTTP while a test records or replays, out of the conftest stub's reach. Passed
through, it is the host module the stub patches.

What it does not change: commands. The workflows issue the same commands with it or without it, so replays match
the same way.

Who removes it: WP-25, together with that I/O, under Option B of D-5, which K-6 chose. Under Option A, WP-28 deletes
the catalog classes, their fixtures, their EXPECTED rows in tests/test_workflow_replay.py and this passthrough
together.
"""


def acceptable(lst: Any, st: Any, uid: int) -> bool:
    """Root or ``uid`` owns the name (``lst``) and the file it points to (``st``), and the file is neither group- nor
    world-writable."""
    return {lst.st_uid, st.st_uid} <= {0, uid} and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def cached_binary(name: str) -> Optional[str]:
    """The first ``name`` in this process's temp dir, then in the shared one; None when neither holds it.

    Raises RuntimeError when that file fails ``acceptable``: the SDK would run it."""
    for path in dict.fromkeys(os.path.join(directory, name) for directory in (tempfile.gettempdir(), SHARED_TMP)):
        try:
            lst = os.lstat(path)
        except FileNotFoundError:
            continue
        if acceptable(lst, os.stat(path), os.getuid()):
            return path
        raise RuntimeError(f"{path} is owned by another user or writable by others, and the Temporal SDK would run "
                           "it; remove it, or make it owned by root or this user and writable by its owner only")
    return None


async def time_skipping() -> WorkflowEnvironment:
    """Temporal's time-skipping test server."""
    return await WorkflowEnvironment.start_time_skipping(
        identity=IDENTITY, test_server_existing_path=cached_binary(f"temporal-test-server-sdk-python-{VERSION}"))


async def local() -> WorkflowEnvironment:
    """Temporal's dev server, a full server without time skipping."""
    return await WorkflowEnvironment.start_local(
        identity=IDENTITY, dev_server_existing_path=cached_binary(f"temporal-sdk-python-{VERSION}"))
