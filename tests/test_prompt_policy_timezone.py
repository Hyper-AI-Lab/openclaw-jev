from app.orchestrator.prompt_policy import (
    USER_TIMEZONE,
    build_generic_execute_prompt,
    user_local_time_block,
)


def test_user_local_time_block_uses_jst():
    block = user_local_time_block()
    assert USER_TIMEZONE == "Asia/Tokyo"
    assert "Japan Standard Time" in block
    assert "Kirill lives in Japan" in block
    assert "appropriate greeting period:" in block


def test_build_generic_execute_prompt_includes_user_local_time():
    prompt = build_generic_execute_prompt(
        user_intent="How are you today?",
        memory_block="",
        context_block="Execute step.",
    )
    assert "USER LOCAL TIME:" in prompt
    assert "How are you today?" in prompt
