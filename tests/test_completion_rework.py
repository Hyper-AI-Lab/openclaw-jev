"""Completion rework loop helpers."""
from app.orchestrator.completion_rework import build_rework_prompt, should_admit_failure


def test_build_rework_prompt_includes_issues():
    prompt = build_rework_prompt(
        "What is the weather?",
        "I don't know",
        evidence_issues=["Response is empty"],
        quality_issues="Too vague",
        attempt=2,
        max_attempts=3,
    )
    assert "EVIDENCE ISSUES" in prompt
    assert "QUALITY ISSUES" in prompt
    assert "Attempt 2 of 3" in prompt


def test_should_admit_failure_at_max():
    assert should_admit_failure(3, 3, "I cannot complete this task") is True


def test_should_admit_failure_on_explicit():
    assert should_admit_failure(1, 3, "I cannot complete this request") is True
