"""Recurrence skip_valid and temporal decay tests."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.task_registry.recurrence import _interval_minutes, should_bypass_intake_llm


def test_should_bypass_health_canary():
    assert should_bypass_intake_llm(["canary"], "RMP CANARY test") is True
    assert should_bypass_intake_llm(["canary", "memory-canary"], "test") is False


def test_interval_minutes_heartbeat():
    assert _interval_minutes("recurrence:heartbeat") >= 1


def test_temporal_decay_orders_fresher_higher():
    from app.task_registry.retriever import _apply_temporal_decay

    hits = [
        {"task_id": "old", "score": 0.9},
        {"task_id": "new", "score": 0.85},
    ]
    registry = [
        {
            "task_id": "old",
            "task_ended_at": (datetime.utcnow() - timedelta(days=60)).isoformat(),
        },
        {
            "task_id": "new",
            "task_ended_at": (datetime.utcnow() - timedelta(days=1)).isoformat(),
        },
    ]
    out = _apply_temporal_decay(hits, registry, half_life_days=30)
    assert out[0]["task_id"] == "new"


@pytest.mark.asyncio
async def test_skip_valid_within_interval():
    from app.task_registry.recurrence import skip_valid_decision

    entry = MagicMock()
    entry.task_id = "tid1"
    entry.task_ended_at = datetime.utcnow() - timedelta(minutes=5)
    scalar = MagicMock()
    scalar.scalar_one_or_none.return_value = entry
    session = AsyncMock()
    session.execute = AsyncMock(return_value=scalar)
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    with patch("app.db.database.AsyncSessionLocal", return_value=cm):
        result = await skip_valid_decision("recurrence:heartbeat")
    assert result is not None
    assert result[0] == "skip_valid"
