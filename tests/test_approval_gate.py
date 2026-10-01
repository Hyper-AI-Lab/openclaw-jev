"""Approval gates read Kirill's own words, remind once after 12 hours and close after 7 days.

The procurement template on Temporal's time-skipping server: its gate sits between the
recommendation and the purchase, so the purchase step running shows the gate let the task through.
"""
import asyncio
from datetime import timedelta
from typing import Any, Dict

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.orchestrator.process_brief import with_catchup
from app.workflows.approval import gate_decision
from app.workflows.catalog_task import CatalogTaskWorkflow
from tests.test_two_phase_answering import ON, QUEUE, CatalogRun, ScriptedRecall, ScriptedStep

BRIEF = "USER CATCH-UP (attach/rebuild):\nKirill said ok to the vendor list, and asked to stop the old order."
GATE_NOTICE = "Procurement approval required"


@pytest.mark.parametrize("reply, decision", [
    ("approve", "approve"), ("Approved!", "approve"), (" deploy. ", "approve"), ("APPROVE", "approve"),
    (with_catchup(BRIEF, "approve"), "approve"),
    ("stop", "stop"), ("please stop", "stop"), ("Cancel.", "stop"), ("reject", "stop"), ("deny", "stop"),
    (with_catchup(BRIEF, "stop"), "stop"),
    ("yes", "other"), ("ok", "other"), ("go ahead", "other"), ("approve it, but use the cheaper vendor", "other"),
    ("don't stop", "other"), (with_catchup(BRIEF, "looks fine to me"), "other"), ("", "other"),
])
def test_only_a_whole_message_from_kirill_decides(reply, decision):
    assert gate_decision(reply) == decision


class GateRun(CatalogRun):
    def __init__(self):
        super().__init__(verdicts=["accept"] * 12)
        self.statuses = []

    def activities(self):
        rec = self

        @activity.defn(name="update_task_status")
        async def update_task_status(payload: Dict[str, Any]) -> None:
            rec.statuses.append(payload.get("status"))

        @activity.defn(name="record_step")
        async def record_step(payload: Dict[str, Any]) -> str:
            return "step-1"

        kept = [fn for fn in super().activities() if fn.__name__ != "update_task_status"]
        return [*kept, update_task_status, record_step]


async def _until(predicate, timeout=30.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


async def _at_gate(rec: GateRun, script) -> Dict[str, Any]:
    payload = {"task_id": "t1", "intent": "Buy two USB-C cables under $20.", "process_type": "procurement",
               "session_key": "agent:main:slack:channel:d0test", "task_type": "procurement", "tags": ["user-request"],
               "rework_max_attempts": 20, "strategy_change_attempt": 10, "escalate_user_attempt": 20,
               "deep_recall": {**ON, "enabled": False}}
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=QUEUE, workflows=[CatalogTaskWorkflow, ScriptedStep, ScriptedRecall],
                          activities=rec.activities()):
            handle = await env.client.start_workflow(CatalogTaskWorkflow.run, payload, id=rec.parent_id, task_queue=QUEUE)
            await _until(lambda: any(GATE_NOTICE in m for m in rec.slack))
            await script(env, handle)
            return await handle.result()


async def test_a_brief_mentioning_stop_or_ok_no_longer_decides():
    rec = GateRun()

    async def script(env, handle):
        await handle.signal("user_input", with_catchup(BRIEF, "looks fine to me"))
        await _until(lambda: any("Approval not recognised" in m for m in rec.slack))
        assert "execute_purchase" not in rec.step_memory
        await handle.signal("user_input", with_catchup(BRIEF, "approve"))

    result = await _at_gate(rec, script)
    assert "execute_purchase" in rec.step_memory and result["status"] != "stopped_by_user"
    assert not any("stopped at approval gate" in m for m in rec.slack)


async def test_a_whole_message_stop_stops_at_the_gate():
    rec = GateRun()

    async def script(env, handle):
        await handle.signal("user_input", with_catchup(BRIEF, "stop"))

    result = await _at_gate(rec, script)
    assert result["status"] == "stopped_by_user" and "execute_purchase" not in rec.step_memory
    assert any("stopped at approval gate" in m for m in rec.slack)


async def test_one_reminder_after_12_hours_and_closed_unapproved_after_7_days():
    rec = GateRun()

    async def script(env, handle):
        await env.sleep(timedelta(hours=11))
        assert not any(m.startswith("Reminder") for m in rec.slack)
        await env.sleep(timedelta(hours=2))
        await _until(lambda: any(m.startswith("Reminder, still waiting for your approval") for m in rec.slack))

    result = await _at_gate(rec, script)
    assert result["status"] == "cancelled" and rec.statuses[-1] == "cancelled"
    assert sum(m.startswith("Reminder") for m in rec.slack) == 1
    assert any("no approval within 7 days" in m for m in rec.slack) and "execute_purchase" not in rec.step_memory
