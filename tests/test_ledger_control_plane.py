"""Control-plane ledger: intake + evaluator events index into registry summaries."""
from types import SimpleNamespace

from app.task_registry.intake_handlers import handle_intake_outcome
from app.task_registry.summary import LEDGER_INDEX_EVENTS, outcome_parts_from_events
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


def test_indexer_payload_includes_evaluator_verdict():
    events = [
        SimpleNamespace(
            event_type="intake.decided",
            event_payload={"decision": "create_fresh"},
        ),
        SimpleNamespace(
            event_type="evaluator.verdict",
            event_payload={"verdict": "accept", "issues": ""},
        ),
        SimpleNamespace(event_type="slack.delivered", event_payload={}),
    ]
    parts = outcome_parts_from_events("completed", events)
    text = "; ".join(parts)
    assert "evaluator=accept" in text
    assert "decision=create_fresh" in text
    assert "slack.delivered" in text
    assert "evaluator.verdict" in LEDGER_INDEX_EVENTS
    assert "intake.clarify" in LEDGER_INDEX_EVENTS
    assert "evaluator.escalate" in LEDGER_INDEX_EVENTS


def test_escalate_verdict_in_index_summary():
    events = [
        SimpleNamespace(
            event_type="evaluator.escalate",
            event_payload={"verdict": "escalate_user", "issues": "blocked on 2FA"},
        )
    ]
    text = "; ".join(outcome_parts_from_events("failed", events))
    assert "evaluator=escalate_user" in text
    assert "blocked on 2FA" in text


@pytest.mark.asyncio
async def test_create_fresh_and_clarify_emit_intake_events():
    db = AsyncMock()
    db.add = MagicMock()
    recorded = []

    def _add(obj):
        recorded.append(getattr(obj, "event_type", type(obj).__name__))

    db.add.side_effect = _add
    with patch(
        "app.task_registry.intake_handlers.record_intake_decision",
        new=AsyncMock(return_value="dec-1"),
    ), patch(
        "app.task_registry.intake_handlers._intake_notify_slack",
        new=AsyncMock(),
    ), patch(
        "app.task_registry.intake_handlers.add_task_message",
        new=AsyncMock(return_value="m1"),
    ):
        out = await handle_intake_outcome(
            {
                "effective_decision": "clarify",
                "decision": "clarify",
                "intake_mode": "enforce",
                "request_hash": "h",
                "confidence": 40,
                "guidance_notes": "which one?",
            },
            request=None,
            db=db,
            intent="unclear ask",
            session_key="agent:main:main",
            tags=["user-request"],
        )
    assert out["intake_action"] == "clarify"
    assert "intake.decided" in recorded
    assert "intake.clarify" in recorded
