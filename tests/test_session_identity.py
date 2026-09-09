from app.task_registry.intake_decision_engine import apply_intake_policy
from app.task_registry.session_identity import (
    active_task_lookup_keys,
    canonical_user_session_key,
    dialogue_lookup_keys,
    discover_slack_conversation_key,
    is_slack_conversation_key,
    persist_user_session_key,
    pick_slack_session_key,
    session_keys_equivalent,
)

STORE = [
    "agent:main:main",
    "agent:main:slack:channel:u0aelfytlks",
    "agent:main:slack:channel:d0ady6n3hpy",
    "agent:main:rmp_task_abc",
]


def test_slack_channel_key_is_conversation():
    assert is_slack_conversation_key("agent:main:slack:channel:D0AELFYTLKS")
    assert is_slack_conversation_key("slack:channel:U0AELFYTLKS")
    assert not is_slack_conversation_key("agent:main:main")
    assert not is_slack_conversation_key("agent:main:rmp_task_abc")


def test_canonical_strips_whitespace():
    assert canonical_user_session_key("  agent:main:slack:channel:D1  ") == (
        "agent:main:slack:channel:D1"
    )


def test_dialogue_lookup_keys_slack_includes_main_backfill():
    keys = dialogue_lookup_keys("agent:main:slack:channel:D1")
    assert keys[0] == "agent:main:slack:channel:D1"
    assert "agent:main:main" in keys


def test_dialogue_lookup_keys_main_stays_main():
    assert dialogue_lookup_keys("agent:main:main") == ["agent:main:main"]


def test_pick_slack_prefers_dm_channel():
    picked = pick_slack_session_key(
        [
            "agent:main:main",
            "agent:main:slack:channel:u0aelfytlks",
            "agent:main:slack:channel:d0ady6n3hpy",
        ]
    )
    assert picked == "agent:main:slack:channel:d0ady6n3hpy"


def test_discover_prefers_dm_over_user_channel():
    key = discover_slack_conversation_key(store_keys=STORE, owner_uid="U0AELFYTLKS")
    assert key == "agent:main:slack:channel:d0ady6n3hpy"


def test_persist_user_rewrites_main_from_store():
    key = persist_user_session_key(
        "agent:main:main",
        task_type="user",
        store_keys=STORE,
    )
    assert key == "agent:main:slack:channel:d0ady6n3hpy"


def test_persist_keeps_canary_on_main():
    key = persist_user_session_key(
        "agent:main:main",
        task_type="canary",
        store_keys=STORE,
    )
    assert key == "agent:main:main"


def test_persist_keeps_reported_slack_key():
    key = persist_user_session_key(
        "agent:main:slack:channel:u0aelfytlks",
        task_type="user",
        store_keys=STORE,
    )
    assert key == "agent:main:slack:channel:u0aelfytlks"


def test_session_keys_equivalent_slack_to_legacy_main():
    assert session_keys_equivalent(
        "agent:main:slack:channel:d0ady6n3hpy", "agent:main:main"
    )
    assert not session_keys_equivalent(
        "agent:main:main", "agent:main:slack:channel:d0ady6n3hpy"
    )
    assert not session_keys_equivalent("agent:main:dm:alice", "agent:main:dm:bob")


def test_active_lookup_main_includes_discovered_slack():
    keys = active_task_lookup_keys(
        "agent:main:main", store_keys=STORE, discover=True
    )
    assert "agent:main:main" in keys
    assert "agent:main:slack:channel:d0ady6n3hpy" in keys


def test_intake_attach_allows_slack_to_legacy_main():
    result = apply_intake_policy(
        {
            "decision": "attach_active",
            "confidence": 99,
            "target_task_id": "abc",
            "rationale": "same conversation",
        },
        {
            "session_key": "agent:main:slack:channel:d0ady6n3hpy",
            "active_tasks": [
                {
                    "task_id": "abc",
                    "session_key": "agent:main:main",
                    "status": "running",
                    "task_type": "user",
                    "goal": "hello",
                }
            ],
        },
        tags=["user-request"],
    )
    assert result["decision"] == "attach_active"
    assert "cross_session_attach_denied" not in result["policy_overrides"]
