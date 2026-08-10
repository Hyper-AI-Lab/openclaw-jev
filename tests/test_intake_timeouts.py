"""Intake timeout budget alignment."""
from app.config import get_intake_timeout_budget


def test_intake_timeout_budget_aligned():
    budget = get_intake_timeout_budget()
    llm = budget["llm_sec"]
    context = budget["context_sec"]
    assert budget["openclaw_poll_sec"] == max(10, llm - 5)
    assert budget["activity_start_to_close_sec"] == llm + context + 20
    assert budget["activity_heartbeat_sec"] == max(55, llm + 5)
    assert budget["workflow_execution_sec"] == llm + context + 45
    assert budget["openclaw_poll_sec"] < budget["activity_start_to_close_sec"]


def test_openclaw_poll_not_exceeds_activity_timeout():
    budget = get_intake_timeout_budget()
    assert budget["openclaw_poll_sec"] <= budget["activity_start_to_close_sec"] - 10
