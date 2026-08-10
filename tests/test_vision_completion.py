"""Tests for vision completion: orchestrator, memory, compensation."""
import pytest

from app.evidence import evidence_high_confidence
from app.orchestrator.decision_engine import decide_completion_gate, decide_step_outcome
from app.orchestrator.prompt_policy import (
    build_generic_execute_prompt,
    resolve_generic_profile,
)


def test_decide_step_outcome_validation_fail_retry():
    d = decide_step_outcome(
        parsed_status="pending",
        reason="bad output",
        validation_ok=False,
        attempt=1,
        max_attempts=3,
    )
    assert d["action"] == "retry"
    assert d["status"] == "pending"


def test_decide_step_outcome_validation_fail_terminal():
    d = decide_step_outcome(
        parsed_status="pending",
        reason="bad output",
        validation_ok=False,
        attempt=3,
        max_attempts=3,
    )
    assert d["action"] == "fail"
    assert d["status"] == "failed"


def test_decide_completion_gate_skip_quality():
    d = decide_completion_gate(
        evidence_passed=True,
        evidence_issues=[],
        quality_passed=None,
        skip_quality_llm=True,
    )
    assert d["action"] == "complete"


def test_resolve_generic_profile_summarize():
    assert resolve_generic_profile("Please summarize the weekly status report") == "summarize"


def test_resolve_generic_profile_read_file():
    assert resolve_generic_profile("read the architecture file") == "memory_first_read"


def test_build_generic_execute_prompt_includes_profile_hint():
    prompt = build_generic_execute_prompt(
        user_intent="summarize docs",
        memory_block="",
        context_block="",
        profile="summarize",
    )
    assert "process memory" in prompt.lower()
    assert "facts" in prompt
    assert "`read`" in prompt


@pytest.mark.asyncio
async def test_generate_plan_conversational_skips_plan_llm(monkeypatch):
    from app.activities import plan_activities

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("send_to_openclaw should not run for conversational mode")

    monkeypatch.setattr(plan_activities, "send_to_openclaw", fail_if_called)
    result = await plan_activities.generate_process_plan(
        {
            "task_id": "t1",
            "intent": "How are you today, Aura?",
            "session_key": "agent:main:main",
            "task_type": "user",
            "execution_mode": "conversational",
        }
    )
    assert result["source"] == "intake_conversational"
    assert len(result["steps"]) == 1
    assert result["steps"][0]["predicate_id"] == "generic_deliver"


@pytest.mark.asyncio
async def test_generate_plan_structured_uses_plan_llm(monkeypatch):
    from app.activities import plan_activities

    async def fake_openclaw(payload):
        return {
            "result": {
                "payloads": [
                    {
                        "text": (
                            '{"steps": [{"name": "read", "kind": "file_read", '
                            '"predicate_id": "file_read", "prompt": "read file"}]}'
                        )
                    }
                ]
            }
        }

    monkeypatch.setattr(plan_activities, "send_to_openclaw", fake_openclaw)
    result = await plan_activities.generate_process_plan(
        {
            "task_id": "t1",
            "intent": "check the new /root/.openclaw/rmp/ARCHITECTURE.md",
            "session_key": "agent:main:main",
            "task_type": "user",
            "execution_mode": "structured_work",
        }
    )
    assert result["source"] == "plan_llm"
    assert result["steps"][0]["predicate_id"] == "file_read"


@pytest.mark.asyncio
async def test_generate_plan_health_canary_single_step(monkeypatch):
    from app.activities import plan_activities

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("health canary should not call plan LLM")

    monkeypatch.setattr(plan_activities, "send_to_openclaw", fail_if_called)
    result = await plan_activities.generate_process_plan(
        {
            "task_id": "t1",
            "intent": "RMP CANARY: Reply with exactly CANARY_OK on its own line. No tools.",
            "session_key": "agent:main:main",
            "task_type": "canary",
            "tags": ["canary", "system"],
            "execution_mode": "structured_work",
        }
    )
    assert result["source"] == "deterministic_health_canary"
    assert len(result["steps"]) == 1


def test_evidence_high_confidence_long_summary():
    intent = "summarize the architecture document"
    response = "A" * 150
    assert evidence_high_confidence(intent, response) is True


@pytest.mark.asyncio
async def test_read_ordered_process_type(monkeypatch):
    from app.memory.router import MemoryRouter

    calls = []

    async def fake_read(scope_type, scope_id, memory_type=None, limit=20, query=None, **kwargs):
        calls.append((scope_type, scope_id, memory_type))
        return []

    monkeypatch.setattr(MemoryRouter, "read", staticmethod(fake_read))
    monkeypatch.setattr(
        "app.memory.graph.query_links",
        __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(return_value=[]),
    )

    await MemoryRouter.read_ordered(
        process_run_id="pr-1",
        process_type="login",
        task_id="t-1",
    )
    assert ("procedural", "login", "procedural") in calls


@pytest.mark.asyncio
async def test_parse_agent_evaluation_orchestrate():
    from app.activities.openclaw_activities import parse_agent_evaluation

    result = await parse_agent_evaluation(
        {
            "text": '{"task_status": "pending", "reason": "need more"}',
            "orchestrate": True,
            "validation_ok": False,
            "attempt": 3,
            "max_attempts": 3,
        }
    )
    assert result["status"] == "failed"
    assert result.get("orchestrator_action") == "fail"
