from app.orchestrator.prompt_policy import (
    USER_TIMEZONE,
    build_generic_execute_prompt,
    user_local_time_block,
)


def test_user_local_time_block_is_a_clock_fact():
    block = user_local_time_block()
    assert USER_TIMEZONE == "Asia/Tokyo"
    assert "USER LOCAL TIME:" in block
    assert "Japan Standard Time" in block
    assert "user clock, not the server clock" in block
    assert "If you greet" not in block
    assert "Forbidden greetings" not in block
    assert "appropriate greeting period" not in block


def test_build_generic_execute_prompt_includes_user_local_time():
    prompt = build_generic_execute_prompt(
        user_intent="How are you today?",
        memory_block="RECENT DIALOGUE (same Slack session — continue this conversation):\nKirill: Are you here\nAura: Good evening",
        context_block="Execute step.",
    )
    assert "USER LOCAL TIME:" in prompt
    assert "How are you today?" in prompt
    assert "continue that conversation" in prompt
    assert "RECENT DIALOGUE" in prompt
    assert "Greetings MUST" not in prompt
    assert "Forbidden greetings" not in prompt
