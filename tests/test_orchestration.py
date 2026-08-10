"""Orchestration tests: plan schema, predicates, facts JSON contract."""
import json

import pytest

from app.orchestrator.step_predicates import (
    decide_status_from_predicates,
    evaluate_step_predicate,
    extract_agent_facts,
)


def test_extract_agent_facts_json():
    text = 'Done.\n{"facts": {"step_complete": true, "file_path": "/root/x.md"}}'
    extracted = extract_agent_facts(text)
    assert extracted["facts"]["step_complete"] is True
    assert extracted["facts"]["file_path"] == "/root/x.md"


def test_file_read_predicate_pass():
    pred = evaluate_step_predicate(
        "file_read",
        user_intent="read architecture.md",
        agent_text="Read /root/.openclaw/rmp/ARCHITECTURE.md successfully.",
        facts={"file_path": "/root/.openclaw/rmp/ARCHITECTURE.md", "read_ok": True},
    )
    assert pred["passed"] is True


def test_summarize_predicate_fail_short():
    pred = evaluate_step_predicate(
        "summarize",
        user_intent="summarize docs",
        agent_text="Short.",
        facts={"step_complete": True},
    )
    assert pred["passed"] is False


def test_decide_status_complete_on_predicate():
    text = "Summary " + ("word " * 30) + '\n{"facts": {"step_complete": true}}'
    decision = decide_status_from_predicates(
        "summarize",
        "summarize the doc",
        text,
        validation_ok=True,
        attempt=1,
        max_attempts=3,
    )
    assert decision["action"] == "complete"
    assert decision["status"] == "completed"


def test_plan_json_schema_roundtrip():
    plan = {
        "steps": [
            {"name": "gather", "predicate_id": "gather_facts", "prompt": "Collect facts"},
            {"name": "deliver", "predicate_id": "generic_deliver", "prompt": "Reply"},
        ]
    }
    raw = json.dumps(plan)
    loaded = json.loads(raw)
    assert len(loaded["steps"]) == 2
    assert loaded["steps"][0]["predicate_id"] == "gather_facts"


@pytest.mark.asyncio
async def test_save_process_plan_activity(monkeypatch):
    written = {}

    class FakeRun:
        id = "pr-1"
        plan_json = None

    class FakeResult:
        def scalar_one_or_none(self):
            return FakeRun()

    class FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, query):
            return FakeResult()

        async def commit(self):
            written["committed"] = True

    monkeypatch.setattr(
        "app.db.database.AsyncSessionLocal",
        lambda: FakeDb(),
    )
    from app.activities.plan_activities import save_process_plan

    ok = await save_process_plan(
        {"process_run_id": "pr-1", "plan": {"steps": [{"name": "a"}]}}
    )
    assert ok is True
    assert written.get("committed") is True


def test_generic_task_has_no_legacy_loop():
    import inspect

    from app.workflows import generic_task

    source = inspect.getsource(generic_task.GenericTaskWorkflow)
    assert "user_evaluation_loop" not in source


@pytest.mark.asyncio
async def test_parse_agent_evaluation_uses_facts():
    from app.activities.openclaw_activities import parse_agent_evaluation

    text = 'Done.\n{"facts": {"step_complete": true}, "task_status": "pending"}'
    result = await parse_agent_evaluation({"text": text})
    assert result["status"] == "completed"
