"""Attempt law: 10 strategy-change, 20 user diagnosis, canaries exempt."""
from app.orchestrator.completion_rework import (
    build_escalation_message,
    build_strategy_change_prompt,
    next_loop_action,
    should_admit_failure,
)
from app.workflows.catalog_task import CatalogTaskWorkflow
from app.workflows.generic_task import GenericTaskWorkflow
import inspect


def test_next_loop_action_1_to_8_rework():
    policy = {
        "max_attempts": 20,
        "strategy_change_attempt": 10,
        "escalate_user_attempt": 20,
    }
    for n in range(1, 9):
        assert next_loop_action(n, policy) == "rework"


def test_next_loop_action_9_through_19_strategy():
    policy = {
        "max_attempts": 20,
        "strategy_change_attempt": 10,
        "escalate_user_attempt": 20,
    }
    assert next_loop_action(9, policy) == "strategy_change"
    assert next_loop_action(10, policy) == "strategy_change"
    assert next_loop_action(19, policy) == "strategy_change"


def test_next_loop_action_20_escalates():
    policy = {
        "max_attempts": 20,
        "strategy_change_attempt": 10,
        "escalate_user_attempt": 20,
    }
    assert next_loop_action(20, policy) == "escalate_user"


def test_nth_attempt_is_judged_not_auto_admitted():
    assert should_admit_failure(20, 20, "here is a revised answer") is False


def test_strategy_prompt_orders_rebuild():
    text = build_strategy_change_prompt("do the thing", "old try", attempt=10)
    assert "STRATEGY CHANGE" in text
    assert "different tools" in text.lower() or "Rebuild" in text


def test_escalation_message_is_user_facing_diagnosis():
    msg = build_escalation_message(
        "upgrade the plugin",
        "still drafting",
        evidence_issues=["missing tests"],
        attempts=20,
    )
    assert "20 times" in msg
    assert "missing tests" in msg
    assert "upgrade the plugin" in msg


def test_generic_canary_skips_rework_loop():
    src = inspect.getsource(GenericTaskWorkflow._judge_and_deliver) + inspect.getsource(
        GenericTaskWorkflow._escalate
    )
    assert 'task_type == "canary"' in src
    assert "max_rework = 0" in src
    assert "next_loop_action" in src
    assert "build_strategy_change_prompt" in src
    assert "build_escalation_message" in src


def test_catalog_honors_strategy_and_escalate():
    src = inspect.getsource(CatalogTaskWorkflow.run)
    assert "next_loop_action" in src
    assert "build_strategy_change_prompt" in src
    assert "build_escalation_message" in src
    assert "should_admit_failure" not in src
