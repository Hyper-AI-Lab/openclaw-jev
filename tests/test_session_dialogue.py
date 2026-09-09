"""Same-session Slack dialogue injection (create_fresh is not amnesia)."""
from datetime import datetime

from app.task_registry.messages import format_session_dialogue
from app.activities.plan_activities import CONVERSATIONAL_DELIVER_STEP


def test_format_session_dialogue_empty():
    assert format_session_dialogue([]) == ""
    assert format_session_dialogue([{"role": "user", "content": "  "}]) == ""


def test_format_session_dialogue_names_kirill_and_aura():
    turns = [
        {
            "role": "user",
            "content": "Are you here aura?",
            "created_at": datetime(2026, 9, 5, 13, 7),
        },
        {
            "role": "assistant",
            "content": "Good evening, Kirill. Yes, I’m here. It’s night in JST.",
            "created_at": datetime(2026, 9, 5, 13, 12),
        },
        {
            "role": "user",
            "content": "How do you feel after changing the LLM?",
            "created_at": datetime(2026, 9, 5, 13, 17),
        },
    ]
    block = format_session_dialogue(turns)
    assert block.startswith("RECENT DIALOGUE")
    assert "Kirill: Are you here aura?" in block
    assert "Aura: Good evening, Kirill." in block
    assert "continue this conversation" in block
    assert "[22:07]" in block
    assert "[13:07]" not in block
    assert "first meeting" not in block


def test_dialogue_lookup_keys_backfill_main_for_slack():
    from app.task_registry.session_identity import dialogue_lookup_keys

    keys = dialogue_lookup_keys("agent:main:slack:channel:D123")
    assert keys[0] == "agent:main:slack:channel:D123"
    assert "agent:main:main" in keys


def test_conversational_deliver_continues_dialogue():
    prompt = CONVERSATIONAL_DELIVER_STEP["prompt"]
    assert "RECENT DIALOGUE" in prompt
    assert "continue that conversation" in prompt
    assert "web-research tools" not in prompt
    assert "crawlers" not in prompt
    assert "JST" not in prompt
    assert "filesystem scans" not in prompt
