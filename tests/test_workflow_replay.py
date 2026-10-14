"""Every recorded workflow history replays on the current code, and every workflow type worker.py registers has one.

EXPECTED pins each fixture file in tests/fixtures: the workflow type it holds and its exact keys, so a history can
neither disappear nor appear silently. A replay whose commands differ from its history raises NondeterminismError, and
a history of a type worker.py does not register fails as well.

The histories are synthetic, recorded by the test harnesses:
- generic_task_histories_before_two_phase.json holds GenericTaskWorkflow histories from before two-phase answering,
  and coding_task_histories.json those of tests/test_coding_workflow.py. Both stay as they were recorded.
- The other six files were recorded on main 434538a, under the identity rmp-tests: generic_task,
  generic_execute_child and intake_workflow by the five named scenarios of tests/test_whole_path.py; catalog_task and
  catalog_step_child by the catalog tests of tests/test_two_phase_answering.py and the gate tests of
  tests/test_approval_gate.py; deep_recall by tests/test_deep_recall.py.

To record, run a scenario's test with RECORD_HISTORIES=<repo>/tests/fixtures. tests/temporal_env.py saves the run
under the scenario's key in the file of its workflow type, and each child or intake run it records under <key>/<n>.
Re-recording replaces the history under a key; a new key goes into EXPECTED in the same change.
"""
import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

import worker
from tests import temporal_env

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Each fixture file: the workflow type it holds and its exact keys.
EXPECTED = {
    "generic_task_histories_before_two_phase.json": (
        "GenericTaskWorkflow", {"accept", "rework_then_accept", "message_after_reply", "stop_while_judging"}),
    "generic_task_histories.json": (
        "GenericTaskWorkflow",
        {"rework_then_accept", "follow_up_folded_in", "stop_during_task", "recall_adds_after_reply", "reconciler_nudge"}),
    "generic_execute_child_histories.json": (
        "GenericExecuteChildWorkflow",
        {"rework_then_accept/1", "follow_up_folded_in/1", "stop_during_task/1", "recall_adds_after_reply/1",
         "reconciler_nudge/1"}),
    "intake_workflow_histories.json": (
        "IntakeWorkflow",
        {"rework_then_accept/1", "follow_up_folded_in/1", "follow_up_folded_in/2", "stop_during_task/1",
         "recall_adds_after_reply/1", "reconciler_nudge/1"}),
    "catalog_task_histories.json": (
        "CatalogTaskWorkflow",
        {"recall_at_step_boundary", "late_recall_let_go", "gate_approve_after_unrecognised_reply", "gate_stop",
         "gate_reminder_then_closed_after_7_days", "real_step_children"}),
    "catalog_step_child_histories.json": (
        "CatalogStepChildWorkflow", {"real_step_children/1", "real_step_children/2", "real_step_children/3"}),
    "deep_recall_histories.json": (
        "DeepRecallWorkflow", {"ready", "needs_no_memory", "nothing_found", "irrelevant", "read_failed_then_retried"}),
    "coding_task_histories.json": (
        "CodingTaskWorkflow",
        {"approve_and_deploy", "rework_then_approve", "stop_mid_run", "change_request_then_approve",
         "usage_limit_pause", "rebase_then_approve"}),
}
CASES = [(name, key) for name, (_type, keys) in EXPECTED.items() for key in sorted(keys)]


def recorded(name):
    return json.loads((FIXTURES / name).read_text())


def history(item):
    return WorkflowHistory.from_json(item["workflow_id"], item["history"])


def test_the_fixture_files_are_exactly_the_expected_ones():
    assert {path.name for path in FIXTURES.glob("*_histories*.json")} == set(EXPECTED)
    assert {name: set(recorded(name)) for name in EXPECTED} == {name: keys for name, (_type, keys) in EXPECTED.items()}


def test_every_history_is_filed_under_its_workflow_type():
    filed = {(name, key): history(item).events[0].workflow_execution_started_event_attributes.workflow_type.name
             for name in EXPECTED for key, item in recorded(name).items()}
    assert filed == {(name, key): EXPECTED[name][0] for name, key in filed}


def test_every_registered_workflow_type_has_a_replay_fixture():
    assert {cls.__name__ for cls in worker.WORKFLOWS} <= {workflow_type for workflow_type, _keys in EXPECTED.values()}


@pytest.mark.parametrize("name, key", CASES, ids=[f"{name}:{key}" for name, key in CASES])
async def test_every_history_replays_on_the_current_code(name, key):
    replayer = Replayer(workflows=worker.WORKFLOWS, workflow_runner=temporal_env.WORKFLOW_RUNNER)
    await replayer.replay_workflow(history(recorded(name)[key]))
