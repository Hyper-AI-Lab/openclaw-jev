from unittest.mock import AsyncMock, patch

import pytest

from app.notify_user import ALLOWED_REASONS, NOTICE_TEXT, deliver_user_notice, notice_task_id


@pytest.mark.asyncio
async def test_intake_unavailable_sends_rmp_slack():
    with (
        patch("app.notify_user.should_suspend_slack", return_value=False),
        patch("app.notify_user.get_slack_bot_token", return_value="xoxb-test"),
        patch("app.notify_user._get_slack_user_id", return_value="U0AELFYTLKS"),
        patch(
            "app.notify_user.send_slack_message_idempotent",
            new=AsyncMock(return_value=True),
        ) as slack,
    ):
        out = await deliver_user_notice(
            session_key="agent:main:slack:channel:d0ady6n3hpy",
            reason="intake_unavailable",
            idempotency_key="abc123",
        )
    assert out["ok"] is True
    assert out["delivered"] is True
    assert out["reason"] == "intake_unavailable"
    slack.assert_awaited_once()
    kwargs = slack.await_args.kwargs
    assert kwargs["user_id"] == "U0AELFYTLKS"
    assert kwargs["message"] == NOTICE_TEXT["intake_unavailable"]
    assert kwargs["task_id"].startswith("notify:intake_unavailable:")


@pytest.mark.asyncio
async def test_stop_idle_sends_rmp_slack():
    with (
        patch("app.notify_user.should_suspend_slack", return_value=False),
        patch("app.notify_user.get_slack_bot_token", return_value="xoxb-test"),
        patch("app.notify_user._get_slack_user_id", return_value="U0AELFYTLKS"),
        patch(
            "app.notify_user.send_slack_message_idempotent",
            new=AsyncMock(return_value=True),
        ) as slack,
    ):
        out = await deliver_user_notice(
            session_key="agent:main:slack:channel:d0ady6n3hpy",
            reason="stop_idle",
            idempotency_key="stop-1",
        )
    assert out["delivered"] is True
    assert slack.await_args.kwargs["message"] == NOTICE_TEXT["stop_idle"]


@pytest.mark.asyncio
async def test_unknown_reason_does_not_slack():
    with patch(
        "app.notify_user.send_slack_message_idempotent",
        new=AsyncMock(return_value=True),
    ) as slack:
        out = await deliver_user_notice(
            session_key="agent:main:slack:channel:d0ady6n3hpy",
            reason="native_fallback",
        )
    assert out["ok"] is False
    assert out["error"] == "unknown_reason"
    slack.assert_not_called()


@pytest.mark.asyncio
async def test_missing_slack_config_fail_closed_no_throw():
    with (
        patch("app.notify_user.should_suspend_slack", return_value=False),
        patch("app.notify_user.get_slack_bot_token", return_value=""),
        patch("app.notify_user._get_slack_user_id", return_value=""),
        patch(
            "app.notify_user.send_slack_message_idempotent",
            new=AsyncMock(return_value=True),
        ) as slack,
    ):
        out = await deliver_user_notice(
            session_key="agent:main:slack:channel:d0ady6n3hpy",
            reason="intake_unavailable",
        )
    assert out["ok"] is True
    assert out["delivered"] is False
    assert out["reason"] == "missing_slack_config"
    slack.assert_not_called()


def test_notice_task_id_stable_and_reasons_whitelisted():
    a = notice_task_id("sess", "intake_unavailable", "k1")
    b = notice_task_id("sess", "intake_unavailable", "k1")
    c = notice_task_id("sess", "intake_unavailable", "k2")
    assert a == b
    assert a != c
    assert ALLOWED_REASONS == {"intake_unavailable", "stop_idle"}


def test_plugin_fail_closed_sends_rmp_notice_not_native():
    from pathlib import Path

    copies = [
        Path("/root/.openclaw/rmp/plugins/rmp_adapter/index.js"),
        Path("/root/.openclaw/plugins/rmp_adapter/index.js"),
    ]
    for path in copies:
        src = path.read_text()
        assert "POST" in src and "/api/notify-user" in src
        assert "intake_unavailable" in src
        assert "stop_idle" in src
        assert "handled: true" in src
        assert "no native" in src.lower() or "no native OpenClaw" in src
        # Error DM happens before the throw that inbound_claim still claims.
        route = src[src.find("function routeSlackDmToRmp") : src.find("function markSlackClaimed")]
        assert "notifyRmpUser" in route
        assert "intake_unavailable" in route
        assert "stop_idle" in route
        assert "/active_user_task" in route
        assert route.find("/active_user_task") < route.find("SIGNALED stop")


def test_active_user_task_endpoint_excludes_canary():
    import inspect

    from app.api.server import get_active_user_task

    src = inspect.getsource(get_active_user_task)
    assert '"canary"' in src
    assert "heartbeat" in src
