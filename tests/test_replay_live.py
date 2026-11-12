"""ops/replay_live.py, the deploy's replay of the running workflows' histories: its replay core on the recorded
histories, its listing of the running workflows and its answer when it cannot list them. Temporal is faked.

The replay core runs the worker's own runner, the default workflow sandbox, as the deploy does. A module a workflow
imports lazily inside that sandbox (build_catalog_step_prompt's app.orchestrator.web_capability) is bound, as the
sandbox's fresh copy, onto its real parent package. So the core runs here in a process of its own, as the deploy runs
the tool: in this process, ``from app.orchestrator import web_capability`` would then give the copy, the conftest's
stub would patch the copy, and a later test would reach the web stack through the module it imported.
"""
import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from temporalio.client import WorkflowHistory

import worker

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def load():
    module_spec = importlib.util.spec_from_file_location("replay_live", ROOT / "ops" / "replay_live.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def recorded():
    """Every recorded history by a unique id, its file and key (two files share workflow ids)."""
    histories = {}
    for path in sorted(FIXTURES.glob("*_histories*.json")):
        for key, item in json.loads(path.read_text()).items():
            history = item["history"]
            histories[f"{path.stem}:{key}"] = json.loads(history) if isinstance(history, str) else history
    return histories


# The tool's replay core on the histories in a JSON file, by id; it prints the core's result.
REPLAY_CORE = """
import asyncio, importlib.util, json, sys
from temporalio.client import WorkflowHistory
spec = importlib.util.spec_from_file_location("replay_live", sys.argv[1])
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
histories = json.loads(open(sys.argv[2]).read())
print(json.dumps(asyncio.run(tool.replay([WorkflowHistory.from_json(k, h) for k, h in histories.items()]))))
"""


def replay_in_a_process(histories, tmp_path):
    """The replay core on ``histories`` in a process of its own, on this interpreter (see the module docstring)."""
    given = tmp_path / "histories.json"
    given.write_text(json.dumps(histories))
    run = subprocess.run([sys.executable, "-c", REPLAY_CORE, str(ROOT / "ops" / "replay_live.py"), str(given)],
                         cwd=ROOT, capture_output=True, text=True, timeout=600,
                         env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert run.returncode == 0, run.stderr[-3000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def submodules_are_their_packages_attributes():
    """Whether every loaded app module is the attribute its parent package names it by, as an import leaves it."""
    return all(getattr(sys.modules[parent], child, module) is module
               for name, module in list(sys.modules.items())
               if name.startswith("app.") and module is not None
               for parent, _, child in [name.rpartition(".")] if parent in sys.modules)


async def test_replay_live_passes_the_recorded_fixtures_and_names_a_broken_one(tmp_path):
    replay_live = load()
    histories = recorded()
    broken = "generic_task_histories:rework_then_accept"
    events = copy.deepcopy(histories[broken])
    scheduled = next(event for event in events["events"] if "activityTaskScheduledEventAttributes" in event)
    scheduled["activityTaskScheduledEventAttributes"]["activityType"]["name"] = "renamed_activity"
    histories[broken] = events

    result = replay_in_a_process(histories, tmp_path)
    # Every recorded history replays on the worker's own runner; the broken one fails, by its id, with one line.
    assert sorted(result["replayed"]) == sorted(set(histories) - {broken})
    assert list(result["failed"]) == [broken]
    error = result["failed"][broken]
    assert "renamed_activity" in error and "\n" not in error and len(error) <= replay_live.ERROR_CHARS
    assert await replay_live.replay([]) == {"replayed": [], "failed": {}}
    # This process's modules are as the tests import them: the conftest's stubs patch the modules the tests use.
    assert submodules_are_their_packages_attributes()


async def test_replay_live_fetches_the_running_workflows_of_the_workers_types(monkeypatch):
    replay_live = load()
    fetched, queries = [], []

    class Handle:
        def __init__(self, workflow_id, run_id):
            self.workflow_id, self.run_id = workflow_id, run_id

        async def fetch_history(self):
            fetched.append((self.workflow_id, self.run_id))
            return WorkflowHistory(self.workflow_id, [])

    class Client:
        async def list_workflows(self, query):
            queries.append(query)
            for workflow_id, workflow_type in (("workflow-1", "GenericTaskWorkflow"),
                                               ("cron-x", "SomeoneElsesWorkflow"),
                                               ("workflow-2-recall", "DeepRecallWorkflow")):
                yield SimpleNamespace(id=workflow_id, run_id=f"run-{workflow_id}", workflow_type=workflow_type)

        def get_workflow_handle(self, workflow_id, *, run_id):
            return Handle(workflow_id, run_id)

    async def connect_temporal():
        return Client()

    monkeypatch.setattr(replay_live, "connect_temporal", connect_temporal)
    histories = await replay_live.running()
    assert queries == ['ExecutionStatus="Running"'] and replay_live.WORKFLOWS is worker.WORKFLOWS
    assert fetched == [("workflow-1", "run-workflow-1"), ("workflow-2-recall", "run-workflow-2-recall")]
    assert [history.workflow_id for history in histories] == ["workflow-1", "workflow-2-recall"]


def test_replay_live_that_cannot_list_exits_2_with_the_error(monkeypatch, capsys):
    replay_live = load()

    async def connect_temporal():
        raise RuntimeError("Failed client connect: Server connection error\nmore detail")

    monkeypatch.setattr(replay_live, "connect_temporal", connect_temporal)
    assert replay_live.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "listing or fetching the running workflows failed: Failed client connect: Server connection error"}
