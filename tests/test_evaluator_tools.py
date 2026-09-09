"""Bounded evaluator situational tools — allow/deny."""
from unittest.mock import AsyncMock, patch

import pytest

from app.orchestrator.evaluator_tools import (
    authorize_evaluator_tool,
    execute_evaluator_tool,
    format_tool_results,
)
from app.orchestrator.process_evaluator import build_evaluator_prompt


def test_health_and_readiness_allowed():
    assert authorize_evaluator_tool("health") is None
    assert authorize_evaluator_tool("readiness") is None
    assert authorize_evaluator_tool("web_capability_status") is None
    assert authorize_evaluator_tool("web_search") is None
    assert authorize_evaluator_tool("jina_reader") is None


def test_privileged_ops_denied():
    assert authorize_evaluator_tool("systemctl") is not None
    assert authorize_evaluator_tool("filesystem_write") is not None
    assert authorize_evaluator_tool("plugin_install") is not None
    assert authorize_evaluator_tool("read_secrets") is not None
    assert authorize_evaluator_tool("/etc/rmp/rmp.env") is not None


@pytest.mark.asyncio
async def test_denied_tool_does_not_run():
    out = await execute_evaluator_tool("systemctl")
    assert out["denied"] is True
    assert out["ok"] is False


def test_evaluator_prompt_includes_tool_results():
    prompt = build_evaluator_prompt(
        {
            "user_intent": "hello",
            "agent_response": "Hi",
            "situational_tools": "- health: ok=True {\"status\": \"ok\"}",
        }
    )
    assert "SITUATIONAL TOOLS" in prompt
    assert "health: ok=True" in prompt


def test_format_marks_denied():
    text = format_tool_results(
        [{"tool": "systemctl", "ok": False, "denied": True, "error": "privileged"}]
    )
    assert "DENIED" in text


@pytest.mark.asyncio
async def test_non_local_url_denied_inside_get():
    from app.orchestrator.evaluator_tools import _local_get

    out = await _local_get("https://example.com/health")
    assert out.get("denied") is True
    assert out.get("ok") is False
