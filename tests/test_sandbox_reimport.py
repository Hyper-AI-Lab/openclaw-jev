"""Temporal's workflow sandbox re-imports a workflow's module from disk for every new run, while a module passed
through keeps the version the worker imported first. This pins that behaviour of temporalio; it is not RMP's code.

So a worker that keeps running while its checkout changes runs new workflow runs on the new workflow code with the
old helpers (``v2|v1``), a mix that no commit ever had. That is why the deploy stops the worker before any file changes
and starts it once the tree has moved (``app.coding.deploy``'s switch window, specs/WP-09.md WP09-08). A temporalio
release that changes the behaviour fails this test; then re-read specs/WP-09.md D-4 before relying on the window
alone or changing it.
"""
import importlib
import os
import sys
import uuid

from temporalio.worker import Worker

from tests import temporal_env

QUEUE = "sandbox-reimport"
WORKFLOW = '''from temporalio import workflow

# By its full name, as RMP's workflows import their activities and helpers: that name is what passes through.
with workflow.unsafe.imports_passed_through():
    import {package}.helper as helper

VERSION = "{version}"


@workflow.defn(name="SandboxReimportProbe")
class SandboxReimportProbe:
    @workflow.run
    async def run(self) -> str:
        return f"{{VERSION}}|{{helper.VERSION}}"
'''


def write_version(package, version, *, later=False):
    """Write the probe's workflow and its helper at ``version``; ``later`` moves both files' mtime 2 s past what
    they had, so no bytecode cached for the old text is taken for the new."""
    for name, text in (("wf.py", WORKFLOW.format(package=package.name, version=version)),
                       ("helper.py", f'VERSION = "{version}"\n')):
        path = package / name
        before = path.stat().st_mtime if path.exists() else None
        path.write_text(text)
        if later:
            os.utime(path, (before + 2, before + 2))


async def test_a_running_worker_runs_new_workflow_code_with_the_helpers_it_started_with(tmp_path, monkeypatch):
    package = tmp_path / f"sandbox_reimport_{uuid.uuid4().hex[:8]}"
    package.mkdir()
    (package / "__init__.py").write_text("")
    write_version(package, "v1")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        probe = importlib.import_module(f"{package.name}.wf").SandboxReimportProbe
        async with await temporal_env.time_skipping() as env:
            async with Worker(env.client, task_queue=QUEUE, workflows=[probe]):
                first = await env.client.execute_workflow(probe.run, id="sandbox-reimport-1", task_queue=QUEUE)
                write_version(package, "v2", later=True)
                second = await env.client.execute_workflow(probe.run, id="sandbox-reimport-2", task_queue=QUEUE)
    finally:
        for name in [name for name in sys.modules if name.split(".")[0] == package.name]:
            del sys.modules[name]
    assert (first, second) == ("v1|v1", "v2|v1")
