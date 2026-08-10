"""Universal task intake tests."""
from app.task_registry.intake_decision_engine import apply_intake_policy
from app.task_registry.intake_prompt import parse_intake_response
from app.task_registry.recurrence import derive_recurrence_key, derive_task_kind


def test_parse_intake_response_json():
    raw = '```json\n{"decision": "wait_active", "confidence": 90, "rationale": "active"}\n```'
    parsed = parse_intake_response(raw)
    assert parsed["decision"] == "wait_active"
    assert parsed["confidence"] == 90


def test_apply_intake_policy_canary_never_skip():
    result = apply_intake_policy(
        {"decision": "skip_noop", "confidence": 99, "rationale": "test"},
        {"intent": "RMP CANARY", "active_tasks": []},
        tags=["canary"],
    )
    assert result["decision"] == "create_fresh"
    assert "canary_never_skip" in result["policy_overrides"]


def test_apply_intake_policy_shadow_mode(monkeypatch):
    monkeypatch.setattr(
        "app.task_registry.intake_decision_engine.get_task_registry_intake_mode",
        lambda: "shadow",
    )
    result = apply_intake_policy(
        {"decision": "skip_noop", "confidence": 99, "rationale": "test"},
        {"intent": "hello", "active_tasks": []},
        tags=["user-request"],
    )
    assert result["effective_decision"] == "create_fresh"
    assert any("shadow_would" in o for o in result["policy_overrides"])


def test_recurrence_key_cron():
    key = derive_recurrence_key(
        "agent:main:cron:abc",
        "[cron:MoltMarket] check notifications",
        ["cron"],
    )
    assert key.startswith("recurrence:cron:")


def test_task_kind_recurrent():
    assert derive_task_kind("recurrence:heartbeat", []) == "recurrent"
    assert derive_task_kind(None, []) == "one_shot"
    assert derive_task_kind(None, ["durable-task"]) == "durable"


def test_health_canary_fast_path():
    from app.task_registry.recurrence import fast_path_decision

    result = fast_path_decision(
        active_tasks=[],
        recurrence_key="recurrence:health_canary",
        tags=["canary"],
        intent="RMP CANARY",
    )
    assert result is not None
    assert result[0] == "create_fresh"


def test_duplicate_intent_attach_active():
    from app.task_registry.recurrence import fast_path_decision

    intent = "Please summarize the quarterly report"
    result = fast_path_decision(
        active_tasks=[
            {
                "task_id": "abc",
                "session_key": "agent:main:main",
                "goal": intent,
            }
        ],
        recurrence_key=None,
        tags=["user-request"],
        intent=intent,
        session_key="agent:main:main",
    )
    assert result is not None
    assert result[0] == "attach_active"
    assert result[2] == "abc"
