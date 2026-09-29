"""PROCESS BRIEF composition for Aura executor prompts."""
from app.orchestrator.process_brief import (
    compose_executor_memory,
    ensure_brief_header,
    format_user_catchup,
)
from app.orchestrator.prompt_policy import build_catalog_step_prompt, build_generic_execute_prompt


def test_compose_keeps_brief_ahead_of_fetched_memory():
    block = compose_executor_memory(
        ensure_brief_header("Relation: new. This is new work."),
        "PROCESS-SCOPED MEMORY:\n- prior fact",
    )
    assert block.index("PROCESS BRIEF") < block.index("PROCESS-SCOPED MEMORY")
    assert "This is new work" in block
    assert "prior fact" in block


def test_ensure_brief_header_idempotent():
    already = "PROCESS BRIEF: continue task abc"
    assert ensure_brief_header(already) == already


def test_catalog_child_prompt_contains_process_brief():
    memory = compose_executor_memory(
        ensure_brief_header(
            "Relation: running. Continue task T. Citations: t-1. Liveness: running."
        ),
        "PROCESS-SCOPED MEMORY:\n- last step drafted plugin",
    )
    prompt = build_catalog_step_prompt(
        user_intent="please self-upgrade and add weather",
        memory_block=memory,
        context_block=format_user_catchup(["also check the logs"]),
        step_prompt="Draft the plugin change.",
    )
    assert "PROCESS BRIEF" in prompt
    assert "Continue task T" in prompt
    assert "last step drafted plugin" in prompt
    assert "USER CATCH-UP" in prompt
    assert "also check the logs" in prompt
    assert "Draft the plugin change" in prompt


def test_catalog_workflow_reads_initial_memory_block():
    import inspect

    from app.workflows.catalog_task import CatalogTaskWorkflow

    src = inspect.getsource(CatalogTaskWorkflow._run)
    assert "initial_memory_block" in src
    assert "compose_executor_memory" in src


def test_generic_prompt_contains_new_work_brief():
    memory = compose_executor_memory(
        ensure_brief_header(
            "new RMP task for this Slack message in the same conversation. "
            "Use RECENT DIALOGUE when present."
        ),
        "",
    )
    prompt = build_generic_execute_prompt(
        user_intent="hello",
        memory_block=memory,
        context_block="",
    )
    assert "PROCESS BRIEF" in prompt
    assert "same conversation" in prompt
    assert "RECENT DIALOGUE" in prompt
    assert "Greetings MUST" not in prompt
    assert "Aura galaxy stack" not in prompt


def test_generic_prompt_does_not_rederive_web_brief_from_regex():
    prompt = build_generic_execute_prompt(
        user_intent="search the web for OpenClaw plugins",
        memory_block="",
        context_block="",
    )
    assert "Aura galaxy stack" not in prompt
    assert "preferred_tools:" not in prompt
    assert "crawl4ai" not in prompt
