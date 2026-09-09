"""Process Evaluator parse + always-gated Slack contract."""
from app.orchestrator.decision_engine import decide_completion_gate
from app.orchestrator.process_evaluator import (
    build_evaluator_prompt,
    parse_evaluator_response,
)
from app.workflows import generic_task
import inspect


def test_malformed_judge_json_is_rework_not_accept():
    result = parse_evaluator_response("not json at all")
    assert result["verdict"] == "rework"
    assert result["quality"] == "fail"
    assert result["parse_error"] is True
    assert "evaluator error" in result["issues"]


def test_judge_error_prefix_fail_closed():
    result = parse_evaluator_response("Error: OpenClaw timeout")
    assert result["quality"] == "fail"
    assert result["verdict"] == "rework"


def test_accept_verdict_maps_quality_pass():
    result = parse_evaluator_response(
        '{"verdict": "accept", "quality": "fail", "reason": "greeting ok"}'
    )
    assert result["verdict"] == "accept"
    assert result["quality"] == "pass"


def test_quality_fail_without_verdict_is_rework():
    result = parse_evaluator_response(
        '{"quality": "fail", "issues": "missing the file read"}'
    )
    assert result["verdict"] == "rework"
    assert result["quality"] == "fail"
    assert "missing the file read" in result["issues"]


def test_evaluator_prompt_is_not_aura():
    prompt = build_evaluator_prompt(
        {
            "user_intent": "hello",
            "agent_response": "Hi Kirill",
            "attempt": 1,
            "process_brief": "PROCESS BRIEF: new work",
        }
    )
    assert "PROCESS EVALUATOR" in prompt
    assert "not Aura" in prompt
    assert "PROCESS BRIEF: new work" in prompt
    assert "hello" in prompt


def test_missing_quality_without_skip_retries():
    d = decide_completion_gate(
        evidence_passed=True,
        evidence_issues=[],
        quality_passed=None,
        skip_quality_llm=False,
    )
    assert d["action"] == "retry"


def test_conversational_no_longer_bypasses_quality_gate():
    source = inspect.getsource(generic_task.GenericTaskWorkflow._plan_driven_loop)
    assert "if is_conversational and clean_result.strip():" not in source
    assert "verify_response_quality" in source
    assert "skip_quality = is_internal_task(user_intent, task_type, tags)" in source
    verify_pos = source.find("verify_response_quality")
    notify_complete = source.find("notify_slack_user", verify_pos)
    assert notify_complete != -1
    assert verify_pos < notify_complete


def test_conversational_still_defers_mid_step_memory():
    source = inspect.getsource(generic_task.GenericTaskWorkflow._plan_driven_loop)
    assert "if not is_conversational:" in source


def test_catalog_evidence_failure_enters_rework_not_instant_fail():
    from app.workflows.catalog_task import CatalogTaskWorkflow

    src = inspect.getsource(CatalogTaskWorkflow.run)
    assert "Completion evidence missing" not in src
    assert "Artifact evidence failed" not in src
    ev_idx = src.find("catalog_evidence = check_catalog_completion")
    fail_idx = src.find("finalize_task_failure", ev_idx)
    rework_idx = src.find("build_rework_prompt", ev_idx)
    assert ev_idx != -1
    assert rework_idx != -1
    assert rework_idx < fail_idx
