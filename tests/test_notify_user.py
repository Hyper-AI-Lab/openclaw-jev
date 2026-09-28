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

    repo = Path(__file__).resolve().parents[1]
    copies = [repo / "plugins/rmp_adapter/index.js"]
    live = Path("/root/.openclaw/plugins/rmp_adapter/index.js")
    try:
        include_live = live.is_file()
    except OSError:
        include_live = False
    if include_live:
        copies.append(live)
    for path in copies:
        src = path.read_text()
        assert "POST" in src and "/api/notify-user" in src
        assert "data.delivered !== true" in src
        assert "RMP user notice not delivered" in src
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
        recovery = route[route.find("Intake POST timed out") :]
        assert "by-idempotency" in recovery
        assert "via active task" not in recovery
        assert "intake_unavailable" in recovery
        msg = src[src.find("api.on('message_received'") : src.find("api.on('reply_payload_sending'")]
        assert "return { handled: true }" in msg
        assert msg.count("handled: true") >= 3
        catch_slice = msg[msg.find("} catch (e)") :]
        assert "handled: true" in catch_slice


def test_plugin_never_blocks_the_gateway_event_loop():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    copies = [repo / "plugins/rmp_adapter/index.js"]
    live = Path("/root/.openclaw/plugins/rmp_adapter/index.js")
    try:
        include_live = live.is_file()
    except OSError:
        include_live = False
    if include_live:
        copies.append(live)
    for path in copies:
        src = path.read_text()
        # The gateway also serves intake's LLM leg; a sync curl froze it for ~70 s.
        assert "child_process" not in src
        assert "execFileSync" not in src
        assert "AbortSignal.timeout" in src
        # OpenClaw ignores a Promise returned from before_message_write.
        assert "api.on('before_message_write', (event, ctx) =>" in src
        for hook in ("inbound_claim", "before_dispatch", "message_received"):
            start = src.find(f"api.on('{hook}'")
            body = src[start : src.find("}, { priority: 120 });", start)]
            assert "routeInBackground(" in body
            assert "routeSlackDmToRmp(" not in body


def test_intake_reservation_is_not_an_active_task():
    from app.task_registry.retriever import include_in_active_snapshot

    assert include_in_active_snapshot({"intake_reserved": True}) is False
    assert include_in_active_snapshot({"intake_reserved": False}) is True
    assert include_in_active_snapshot({}) is True
    assert include_in_active_snapshot(None) is True


def test_second_post_does_not_start_another_intake_while_reserved():
    from datetime import datetime, timedelta

    from app.api.server import reservation_retry_should_run_intake

    class Row:
        status = "created"
        supplementary_context = {"intake_reserved": True}
        updated_at = datetime.utcnow()
        created_at = updated_at

    assert reservation_retry_should_run_intake(Row()) is False
    stale = Row()
    stale.updated_at = datetime.utcnow() - timedelta(seconds=151)
    assert reservation_retry_should_run_intake(stale) is True
    worked = Row()
    worked.supplementary_context = {}
    assert reservation_retry_should_run_intake(worked) is False


def test_plugin_post_timeout_covers_intake_budget():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    src = (repo / "plugins/rmp_adapter/index.js").read_text()
    assert "intakePostTimeoutSec" in src
    assert "intake_llm_timeout_sec" in src
    assert "intake_vector_deadline_sec" in src
    assert "llm + ctx + 45 + 30" in src
    server = (repo / "app/api/server.py").read_text()
    create = server[server.find("async def create_task") :]
    reserve_at = create.find('supplementary_context={"intake_reserved": True}')
    intake_at = create.find("run_classify_task_intake")
    assert reserve_at != -1 and intake_at != -1
    assert reserve_at < intake_at
    from app.api.server import task_visible_for_idempotent_recovery

    assert task_visible_for_idempotent_recovery("running") is True
    assert task_visible_for_idempotent_recovery("created") is True
    assert task_visible_for_idempotent_recovery("completed") is False
    assert task_visible_for_idempotent_recovery("stopped_by_user") is False
    assert task_visible_for_idempotent_recovery(None) is False


def test_active_user_task_endpoint_excludes_canary():
    import inspect

    from app.api.server import get_active_user_task

    src = inspect.getsource(get_active_user_task)
    assert '"canary"' in src
    assert "heartbeat" in src
