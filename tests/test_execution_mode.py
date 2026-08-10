"""Tests for intake execution_mode classification."""
from app.orchestrator.execution_mode import (
    EXECUTION_CONVERSATIONAL,
    EXECUTION_STRUCTURED,
    infer_degraded_execution_mode,
    intake_llm_failed,
    normalize_execution_mode,
    resolve_execution_mode,
)
from app.task_registry.intake_decision_engine import apply_intake_policy


def test_normalize_execution_mode_aliases():
    assert normalize_execution_mode("conversational") == EXECUTION_CONVERSATIONAL
    assert normalize_execution_mode("chat") == EXECUTION_CONVERSATIONAL
    assert normalize_execution_mode("structured_work") == EXECUTION_STRUCTURED
    assert normalize_execution_mode("work") == EXECUTION_STRUCTURED
    assert normalize_execution_mode("bogus") is None


def test_resolve_execution_mode_prefers_intake_llm():
    mode = resolve_execution_mode(
        intent="How are you today?",
        tags=["user-request"],
        task_type="user",
        llm_mode="conversational",
    )
    assert mode == EXECUTION_CONVERSATIONAL


def test_resolve_execution_mode_defaults_structured_without_llm():
    mode = resolve_execution_mode(
        intent="Read ARCHITECTURE.md and summarize every module",
        tags=["user-request"],
        task_type="user",
    )
    assert mode == EXECUTION_STRUCTURED


def test_apply_intake_policy_includes_execution_mode():
    result = apply_intake_policy(
        {
            "decision": "create_fresh",
            "confidence": 90,
            "execution_mode": "conversational",
            "rationale": "chat",
        },
        {"intent": "Hello Aura", "active_tasks": [], "task_type": "user"},
        tags=["user-request"],
    )
    assert result["execution_mode"] == EXECUTION_CONVERSATIONAL


KIRILL_MSG = (
    "I am sorry for being quiet here, aura, I am just trying to strengthen your RMP "
    "and other things like temporal stack and also we have phoenix (projects manager), "
    "so the development goes quite extensively actually. I am not sure if you have the "
    "full access to the codebase on this server, but if you, you can explore it yourself "
    "and see how much it was made."
)


def test_kirill_dev_update_classified_conversational_when_intake_says_so():
    result = apply_intake_policy(
        {
            "decision": "create_fresh",
            "confidence": 88,
            "execution_mode": "conversational",
            "rationale": "status update, no concrete deliverable",
        },
        {"intent": KIRILL_MSG, "active_tasks": [], "task_type": "user"},
        tags=["user-request"],
    )
    assert result["execution_mode"] == EXECUTION_CONVERSATIONAL


def test_intake_llm_failed_detects_error_markers():
    assert intake_llm_failed({"confidence": 0, "rationale": "Intake LLM error: timeout"})
    assert intake_llm_failed(
        {"confidence": 0, "rationale": "Intake workflow unavailable; deterministic fallback only"}
    )
    assert not intake_llm_failed(None)
    assert not intake_llm_failed({"confidence": 90, "rationale": "ok"})


def test_degraded_execution_mode_user_dm_when_intake_llm_failed():
    mode = resolve_execution_mode(
        intent=KIRILL_MSG,
        tags=["user-request"],
        task_type="user",
        llm_result={
            "confidence": 0,
            "rationale": "Intake LLM error: Not in activity context",
        },
    )
    assert mode == EXECUTION_CONVERSATIONAL


def test_degraded_execution_mode_stays_structured_for_canary():
    mode = infer_degraded_execution_mode(
        intent="RMP CANARY",
        tags=["canary"],
        task_type="canary",
        intake_llm_failed_flag=True,
    )
    assert mode == EXECUTION_STRUCTURED
