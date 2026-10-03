"""Slack delivery: long replies go out whole, transient errors retry, permanent ones are recorded."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.activities import side_effects
from app.activities.side_effects import SlackTransientError, send_slack_message_idempotent, split_for_slack

REAL_CLIENT = httpx.AsyncClient
PARAGRAPH = ("Osaka in October is mild, usually between 18 and 24 degrees with some rain. " * 15).strip()
LONG = "\n\n".join(f"Section {i}. {PARAGRAPH}" for i in range(8))


def test_long_messages_split_at_paragraphs_without_losing_text():
    parts = split_for_slack(LONG)
    assert len(parts) > 1 and all(len(p) <= side_effects.SLACK_PART_CHARS for p in parts)
    assert all(p.startswith("Section") for p in parts)
    assert "\n\n".join(parts) == LONG


class FakeSlack:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posted = []

    def client(self, *args, **kwargs):
        def handler(request):
            self.posted.append(request.read().decode())
            status, body, headers = self.responses.pop(0) if self.responses else (200, {"ok": True}, {})
            return httpx.Response(status, json=body, headers=headers)

        return REAL_CLIENT(transport=httpx.MockTransport(handler))


@pytest.fixture
def ledger(monkeypatch):
    receipts, added = set(), []

    async def already_sent(key):
        return key in receipts

    async def record_receipt(key, effect_type, metadata=None):
        receipts.add(key)

    db = MagicMock()
    db.add = lambda obj: added.append(obj)
    db.commit = AsyncMock()
    db.get = AsyncMock(return_value=object())  # the task exists: a real task's reply is a conversation message
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(side_effects, "_already_sent", already_sent)
    monkeypatch.setattr(side_effects, "_record_receipt", record_receipt)
    monkeypatch.setattr(side_effects, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(side_effects.asyncio, "sleep", AsyncMock())
    return receipts, added


async def _send(slack, message="Hello", alert=None):
    with patch.object(side_effects.httpx, "AsyncClient", slack.client), \
         patch("app.production.alerting.send_alert", alert or AsyncMock(return_value=True)):
        return await send_slack_message_idempotent("t1", "U1", message, "xoxb-test")


async def test_a_transient_failure_is_retried_until_slack_accepts(ledger):
    slack = FakeSlack([(503, {"ok": False}, {}), (200, {"ok": False, "error": "internal_error"}, {}),
                       (200, {"ok": True, "ts": "1790.0042"}, {})])
    assert await _send(slack) is True
    assert len(slack.posted) == 3
    assert [type(o).__name__ for o in ledger[1]] == ["Event", "TaskMessage"]
    assert ledger[1][1].slack_ts == "1790.0042"


async def test_a_lasting_outage_raises_so_the_activity_retries_later(ledger):
    slack = FakeSlack([(503, {"ok": False}, {})] * 3)
    with pytest.raises(SlackTransientError):
        await _send(slack)
    assert ledger[1] == []


async def test_retry_after_is_honoured(ledger):
    slack = FakeSlack([(429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "7"}), (200, {"ok": True}, {})])
    assert await _send(slack) is True
    side_effects.asyncio.sleep.assert_awaited_with(7.0)


async def test_a_permanent_error_is_recorded_and_alerted_not_retried(ledger):
    alert = AsyncMock(return_value=True)
    slack = FakeSlack([(200, {"ok": False, "error": "channel_not_found"}, {})])
    assert await _send(slack, alert=alert) is False
    assert len(slack.posted) == 1
    events = [o for o in ledger[1] if type(o).__name__ == "Event"]
    assert events[0].event_type == "slack.delivery_failed" and events[0].event_payload["error"] == "channel_not_found"
    alert.assert_awaited_once()


async def test_a_long_reply_goes_out_in_order_and_a_retry_sends_only_the_missing_parts(ledger):
    parts = split_for_slack(LONG)
    failing = FakeSlack([(200, {"ok": True}, {})] + [(503, {"ok": False}, {})] * 3)
    with pytest.raises(SlackTransientError):
        await _send(failing, LONG)
    retry = FakeSlack([])
    assert await _send(retry, LONG) is True
    import json

    sent = [json.loads(body)["text"] for body in failing.posted[:1] + retry.posted]
    assert len(parts) == 3 and len(retry.posted) == len(parts) - 1
    assert sent == parts
    messages = [o for o in ledger[1] if type(o).__name__ == "TaskMessage"]
    assert len(messages) == 1 and messages[0].content == LONG
