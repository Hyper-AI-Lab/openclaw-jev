"""Additional intake handler and spawn tests."""
import pytest

from app.orchestrator.completion_rework import should_admit_failure


def test_should_admit_failure_on_max_attempts():
    assert should_admit_failure(3, 3, "still trying") is True


def test_should_admit_failure_on_explicit_admission():
    assert should_admit_failure(1, 3, "I cannot complete this request.") is True
    assert should_admit_failure(1, 3, "Here is the answer.") is False


def test_apply_intake_policy_cross_session_denied():
    from app.task_registry.intake_decision_engine import apply_intake_policy

    result = apply_intake_policy(
        {
            "decision": "attach_active",
            "confidence": 99,
            "target_task_id": "abc",
            "rationale": "same intent",
        },
        {
            "session_key": "agent:main:dm:alice",
            "active_tasks": [
                {
                    "task_id": "abc",
                    "session_key": "agent:main:dm:bob",
                }
            ],
        },
        tags=["user-request"],
    )
    assert result["decision"] == "create_fresh"
    assert "cross_session_attach_denied" in result["policy_overrides"]
