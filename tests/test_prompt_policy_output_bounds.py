from app.orchestrator.prompt_policy import (
    OUTPUT_BOUNDS_GUIDANCE,
    build_catalog_step_prompt,
    build_generic_execute_prompt,
)


def test_guidance_says_bound_output_do_not_rerun_and_leave_long_output_to_claude():
    text = OUTPUT_BOUNDS_GUIDANCE
    for tool in ("head", "tail", "grep", "wc -l"):
        assert tool in text
    assert "12,000 characters" in text
    assert "Do not re-run a command whose output is already in this conversation" in text
    assert "diffs and long output" in text and "claude_start" in text and "claude_send" in text
    assert "saved to a file" in text


def test_every_execution_prompt_carries_the_guidance():
    generic = build_generic_execute_prompt(user_intent="x", memory_block="", context_block="")
    catalog = build_catalog_step_prompt(user_intent="x", memory_block="", context_block="", step_prompt="do it")
    assert OUTPUT_BOUNDS_GUIDANCE in generic
    assert OUTPUT_BOUNDS_GUIDANCE in catalog
    # It sits with the other standing rules, before the numbered instructions.
    assert generic.index(OUTPUT_BOUNDS_GUIDANCE) < generic.index("Instructions:")
    assert catalog.index(OUTPUT_BOUNDS_GUIDANCE) < catalog.index("Instructions:")
