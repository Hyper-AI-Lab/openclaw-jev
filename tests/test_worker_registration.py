"""The production worker registers every workflow and activity the workflows use, and nothing changed it silently."""
import ast
import importlib
from pathlib import Path

from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import worker

REPO = Path(__file__).resolve().parents[1]
ACTIVITY_CALLS = {"execute_activity", "start_activity", "execute_local_activity", "start_local_activity"}
CHILD_CALLS = {"execute_child_workflow", "start_child_workflow"}


def workflow_modules():
    """Each module of app/workflows with its parsed source."""
    for path in sorted((REPO / "app" / "workflows").glob("*.py")):
        name = "app.workflows" if path.stem == "__init__" else f"app.workflows.{path.stem}"
        yield importlib.import_module(name), ast.parse(path.read_text(), str(path))


def test_the_worker_registers_what_it_registered_before():
    """worker.py's lists at main 5424c02; a change to registration updates this pin deliberately."""
    assert [cls.__name__ for cls in worker.WORKFLOWS] == [
        "GenericTaskWorkflow", "CatalogTaskWorkflow", "CatalogStepChildWorkflow", "GenericExecuteChildWorkflow",
        "IntakeWorkflow", "DeepRecallWorkflow", "CodingTaskWorkflow",
    ]
    assert [fn.__name__ for fn in worker.ACTIVITIES] == [
        "send_to_openclaw", "task_actions_digest", "validate_openclaw_output", "parse_agent_evaluation",
        "update_task_status", "notify_slack_user", "deliver_reply_files", "check_intermediate_updates_enabled",
        "verify_response_quality", "ensure_process_run", "acquire_process_run_lease", "release_process_run_lease",
        "finalize_task_failure", "execute_compensation", "update_process_state", "record_step", "record_observation",
        "record_event", "confirm_approval_provenance", "write_process_memory", "read_process_memory",
        "build_process_memory_context", "write_episodic_observation", "compact_episodic_memory",
        "promote_completion_memory", "register_artifact", "list_process_artifacts", "generate_process_plan",
        "save_process_plan", "classify_task_intake_activity", "resubmit_user_messages",
        # RECALL_ACTIVITIES
        "start_recall_report", "plan_recall_step", "retrieve_recall_step", "read_recall_step", "close_recall_report",
        "judge_recall_novelty", "settle_recall_report",
        # CODING_ACTIVITIES
        "coding_settings", "acquire_coding_slot", "release_coding_slot", "draft_coding_brief",
        "prepare_coding_workspace", "run_claude_round", "stop_coding_units", "verify_coding_round",
        "review_coding_round", "deploy_coding_change", "refresh_coding_base",
    ]


def test_every_workflow_and_activity_the_workflows_use_is_registered():
    activities, children, defined = [], [], set()
    for module, tree in workflow_modules():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "workflow"):
                continue
            where = f"{module.__name__}:{node.lineno}"
            if node.func.attr in ACTIVITY_CALLS:
                assert isinstance(node.args[0], ast.Name), f"{where}: the activity is not named by a plain name"
                activities.append((where, getattr(module, node.args[0].id)))
            elif node.func.attr in CHILD_CALLS:
                run = node.args[0]
                assert isinstance(run, ast.Attribute) and isinstance(run.value, ast.Name), \
                    f"{where}: the child workflow is not named as <class>.run"
                children.append((where, getattr(module, run.value.id)))
        defined |= {value for value in vars(module).values() if isinstance(value, type)
                    and value.__module__ == module.__name__ and workflow._Definition.from_class(value)}
    assert activities and children, "the walk found no activity or child workflow call"
    assert [where for where, fn in activities if fn not in worker.ACTIVITIES] == []
    assert [where for where, cls in children if cls not in worker.WORKFLOWS] == []
    assert defined == set(worker.WORKFLOWS)


async def test_main_builds_the_worker_from_the_module_lists(monkeypatch):
    from app.decisions import jev
    from app.deep_memory import curator
    from app.llm import openai_direct
    from app.production import runtime_sync

    client, built = object(), []

    class Recorder:
        def __init__(self, client, **kwargs):
            built.append((client, kwargs))

        async def run(self):
            return None

    async def connect():
        return client

    async def nothing():
        return None

    monkeypatch.setattr(worker, "connect_temporal_with_retry", connect)
    monkeypatch.setattr(worker, "init_telemetry", lambda name: None)
    monkeypatch.setattr(worker, "Worker", Recorder)
    monkeypatch.setattr(runtime_sync, "mark_runtime_boot", lambda name: None)
    monkeypatch.setattr(curator, "warm_up", nothing)
    monkeypatch.setattr(jev, "close_jev_client", nothing)
    monkeypatch.setattr(openai_direct, "close_direct_clients", nothing)

    await worker.main()

    [(got, kwargs)] = built
    assert got is client
    assert kwargs["task_queue"] == "openclaw-tasks"
    assert kwargs["workflows"] is worker.WORKFLOWS and kwargs["activities"] is worker.ACTIVITIES


async def test_the_production_worker_builds_on_a_test_server():
    """Construction runs the SDK's checks: duplicate names, and every workflow validated in the sandbox.

    The worker is shut down before the server goes: a worker that was only constructed keeps polling openclaw-tasks
    on the dead server's port for the rest of the session, where a later test server may listen.
    """
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="openclaw-tasks", workflows=worker.WORKFLOWS,
                          activities=worker.ACTIVITIES):
            pass
