"""Process memory daily-use tests and prompt contract."""
import pytest
from httpx import ASGITransport, AsyncClient

from app.orchestrator.prompt_policy import (
    MEMORY_FIRST_UNIVERSAL,
    build_catalog_step_prompt,
    build_generic_execute_prompt,
)


def test_executor_prompts_forbid_workspace_memory_search():
    generic = build_generic_execute_prompt(
        user_intent="read file",
        memory_block="PROCESS-SCOPED MEMORY:\n- prior fact",
        context_block="",
    )
    catalog = build_catalog_step_prompt(
        user_intent="register",
        memory_block="PROCESS-SCOPED MEMORY:\n- prior",
        context_block="",
        step_prompt="Complete registration",
    )
    assert MEMORY_FIRST_UNIVERSAL in generic
    assert "memory_search" in generic.lower()
    assert MEMORY_FIRST_UNIVERSAL in catalog
    assert "facts" in generic.lower()
    assert "facts" in catalog.lower()


@pytest.mark.asyncio
async def test_memory_process_context_endpoint_fail_soft(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("vector down")

    monkeypatch.setattr(
        "app.memory.router.MemoryRouter.build_context_block",
        staticmethod(boom),
    )
    monkeypatch.setattr(
        "app.memory.router.MemoryRouter.read_ordered",
        staticmethod(boom),
    )
    monkeypatch.setattr("app.api.server.get_api_key", lambda: "")

    from app.api.server import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/memory/process/pr-test/context")

    assert resp.status_code == 200
    body = resp.json()
    assert body["context_block"] == ""
    assert body["count"] == 0


def test_transcript_memory_first_helper():
    """Helper for canary: detect process-scoped memory usage vs workspace-first search."""
    good = "Using PROCESS-SCOPED MEMORY for prior context."
    bad = "Let me memory_search the workspace first."
    assert "PROCESS-SCOPED MEMORY" in good
    assert "memory_search" not in good.lower() or "do not use memory_search" in good.lower()
    assert "memory_search" in bad.lower()
