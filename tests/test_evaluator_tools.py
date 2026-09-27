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


@pytest.mark.asyncio
async def test_chat_context_skips_web_stack(monkeypatch):
    from app.orchestrator.evaluator_tools import collect_situational_context

    called = []

    async def fake_exec(name, *, intent=""):
        called.append(name)
        return {"tool": name, "ok": True, "body": {}}

    monkeypatch.setattr(
        "app.orchestrator.evaluator_tools.execute_evaluator_tool", fake_exec
    )
    monkeypatch.setattr(
        "app.orchestrator.evaluator_tools._log_tool_use", AsyncMock()
    )
    await collect_situational_context({"user_intent": "hello"})
    assert called == ["health", "readiness"]


@pytest.mark.asyncio
async def test_web_intent_probes_capability_status(monkeypatch):
    from app.orchestrator.evaluator_tools import collect_situational_context

    called = []

    async def fake_exec(name, *, intent=""):
        called.append(name)
        return {"tool": name, "ok": True, "body": {}}

    monkeypatch.setattr(
        "app.orchestrator.evaluator_tools.execute_evaluator_tool", fake_exec
    )
    monkeypatch.setattr(
        "app.orchestrator.evaluator_tools._log_tool_use", AsyncMock()
    )
    await collect_situational_context(
        {"user_intent": "search the web for OpenClaw plugins"}
    )
    assert "web_capability_status" in called
    assert called[0:2] == ["health", "readiness"]


@pytest.mark.asyncio
async def test_web_capability_status_stack_down_is_not_ok(monkeypatch):
    async def fake_get(url, headers=None):
        return {"ok": False, "denied": False, "error": "down"}

    monkeypatch.setattr(
        "app.orchestrator.evaluator_tools._local_get", fake_get
    )
    monkeypatch.setattr(
        "app.orchestrator.web_capability.obscura_available", lambda: False
    )
    out = await execute_evaluator_tool(
        "web_capability_status", intent="search the web for x"
    )
    assert out["ok"] is False
    assert out["obscura_available"] is False


@pytest.mark.asyncio
async def test_web_search_calls_local_search_route(monkeypatch):
    seen = []

    async def fake_get(url, headers=None):
        seen.append(url)
        return {"ok": True, "status": 200, "body": {"ok": True, "results": []}}

    monkeypatch.setattr("app.orchestrator.evaluator_tools._local_get", fake_get)
    out = await execute_evaluator_tool("web_search", intent="latest weather")
    assert seen and "/v1/search?q=" in seen[0]
    assert "/v1/jina" not in seen[0]
    assert out["ok"] is True


@pytest.mark.asyncio
async def test_web_search_body_failure_is_not_ok(monkeypatch):
    async def fake_get(url, headers=None):
        return {"ok": True, "status": 200, "body": {"ok": False, "error": "search key missing"}}

    monkeypatch.setattr("app.orchestrator.evaluator_tools._local_get", fake_get)
    out = await execute_evaluator_tool("web_search", intent="latest weather")
    assert out["ok"] is False


@pytest.mark.asyncio
async def test_jina_reader_calls_local_jina_route(monkeypatch):
    seen = []

    async def fake_get(url, headers=None):
        seen.append(url)
        return {"ok": True, "status": 200, "body": {"ok": True, "markdown": "hi"}}

    monkeypatch.setattr("app.orchestrator.evaluator_tools._local_get", fake_get)
    out = await execute_evaluator_tool(
        "jina_reader", intent="read https://example.com/docs"
    )
    assert seen and "/v1/jina?url=" in seen[0]
    assert out["ok"] is True
