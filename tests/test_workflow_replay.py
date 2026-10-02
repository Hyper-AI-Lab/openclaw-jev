"""Histories recorded by earlier workflow code must replay on the current code (no nondeterminism)."""
import json
from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from app.workflows.coding_task import CodingTaskWorkflow
from app.workflows.generic_task import GenericTaskWorkflow

FIXTURES = Path(__file__).resolve().parent / "fixtures"


async def test_generic_task_histories_from_before_two_phase_answering_replay():
    recorded = json.loads((FIXTURES / "generic_task_histories_before_two_phase.json").read_text())
    replayer = Replayer(workflows=[GenericTaskWorkflow])
    assert set(recorded) == {"accept", "rework_then_accept", "message_after_reply", "stop_while_judging"}
    for item in recorded.values():
        await replayer.replay_workflow(WorkflowHistory.from_json(item["workflow_id"], item["history"]))


async def test_coding_task_histories_replay():
    """Recorded by tests/test_coding_workflow.py with RECORD_CODING_HISTORIES=<this fixture>."""
    recorded = json.loads((FIXTURES / "coding_task_histories.json").read_text())
    replayer = Replayer(workflows=[CodingTaskWorkflow])
    assert set(recorded) == {"approve_and_deploy", "rework_then_approve", "stop_mid_run", "change_request_then_approve",
                             "usage_limit_pause", "rebase_then_approve"}
    for item in recorded.values():
        await replayer.replay_workflow(WorkflowHistory.from_json(item["workflow_id"], item["history"]))
