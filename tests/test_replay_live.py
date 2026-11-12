"""ops/replay_live.py, the deploy's replay of the running workflows' histories: its replay core on the recorded
histories, its listing of the running workflows and its answer when it cannot list them. Temporal is faked."""
import copy
import importlib.util
import json
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


async def test_replay_live_passes_the_recorded_fixtures_and_names_a_broken_one():
    replay_live = load()
    histories = recorded()
    broken = "generic_task_histories:rework_then_accept"
    events = copy.deepcopy(histories[broken])
    scheduled = next(event for event in events["events"] if "activityTaskScheduledEventAttributes" in event)
    scheduled["activityTaskScheduledEventAttributes"]["activityType"]["name"] = "renamed_activity"
    histories[broken] = events

    result = await replay_live.replay([WorkflowHistory.from_json(key, history) for key, history in histories.items()])
    # Every recorded history replays on the worker's own runner; the broken one fails, by its id, with one line.
    assert sorted(result["replayed"]) == sorted(set(histories) - {broken})
    assert list(result["failed"]) == [broken]
    error = result["failed"][broken]
    assert "renamed_activity" in error and "\n" not in error and len(error) <= replay_live.ERROR_CHARS
    assert await replay_live.replay([]) == {"replayed": [], "failed": {}}


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
            for workflow_id, workflow_type in (("workflow-1", "GenericTaskWorkflow"), ("cron-x", "SomeoneElsesWorkflow"),
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
