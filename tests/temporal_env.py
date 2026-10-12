"""Temporal for the tests: the test environments every Temporal test starts from, the sandbox runner the replays
share, and the recorder of replay fixtures.

The SDK runs the server binary it finds under a fixed name in the temp dir, and downloads it there when there is none.
``cached_binary`` hands the SDK a cached binary only when root or this user owns both the name and the file it points
to, and no group or other user can write that file. A cached binary that fails the rule fails the test instead,
because the SDK would run that same file. With none cached, the SDK downloads, as it does in CI.

``record`` and ``record_run`` save workflow histories as replay fixtures (tests/test_workflow_replay.py) when
RECORD_HISTORIES names a directory, and do nothing otherwise.
"""
import importlib.metadata
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

from temporalio.client import Client, WorkflowHistory
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


def _without_stack_traces(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_stack_traces(item) for key, item in value.items() if key != "stackTrace"}
    if isinstance(value, list):
        return [_without_stack_traces(item) for item in value]
    return value


async def record(client: Client, key: str, workflow_id: str) -> Optional[WorkflowHistory]:
    """Save the workflow's history under ``key`` in its type's fixture file, ``<workflow module>_histories.json`` in
    the directory RECORD_HISTORIES names, without stack traces. Recording a key again replaces its history. Returns
    the history, or None when RECORD_HISTORIES is unset."""
    directory = os.environ.get("RECORD_HISTORIES")
    if not directory:
        return None
    # Imported here: a test module that defines a sandboxed workflow is imported again inside the sandbox, this module
    # with it, and the sandbox refuses what worker's activities import.
    import worker

    history = await client.get_workflow_handle(workflow_id).fetch_history()
    name = history.events[0].workflow_execution_started_event_attributes.workflow_type.name
    types = {cls.__name__: cls for cls in worker.WORKFLOWS}
    if name not in types:
        raise LookupError(f"{workflow_id} is a {name}, which worker.py does not register")
    path = Path(directory) / f"{types[name].__module__.rsplit('.', 1)[-1]}_histories.json"
    saved = json.loads(path.read_text()) if path.exists() else {}
    saved[key] = {"workflow_id": history.workflow_id, "history": _without_stack_traces(history.to_json_dict())}
    path.write_text(json.dumps(saved, separators=(",", ":")) + "\n")
    return history


async def record_run(client: Client, scenario: str, workflow_id: str, children: Iterable[type] = ()) -> None:
    """Record the run under ``scenario``, and each child of a class in ``children`` under ``<scenario>/<n>``, counting
    from 1 per class in the order the run started them.

    Pass only real workflow classes: a scripted stand-in shares its real class's type name, so its history would be
    filed, and replayed, as the real class's."""
    history = await record(client, scenario, workflow_id)
    if history is None:
        return
    started = [event.child_workflow_execution_started_event_attributes for event in history.events
               if event.HasField("child_workflow_execution_started_event_attributes")]
    for cls in children:
        ids = [child.workflow_execution.workflow_id for child in started if child.workflow_type.name == cls.__name__]
        for n, child_id in enumerate(ids, start=1):
            await record(client, f"{scenario}/{n}", child_id)
