"""Deterministic skip_noop fast path tests."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.task_registry.recurrence import _is_noop_outcome, skip_noop_decision, supersede_decision


def test_is_noop_outcome_phrases():
    assert _is_noop_outcome("Nothing new on MoltMarket. Stay quiet.") is True
    assert _is_noop_outcome("Found 3 important job updates for you.") is False


@pytest.mark.asyncio
async def test_skip_noop_within_interval():
    entry = MagicMock()
    entry.task_id = "tid-noop"
    entry.task_ended_at = datetime.utcnow() - timedelta(minutes=10)
    entry.outcome_summary = "Nothing new; no actionable items."
    scalar = MagicMock()
    scalar.scalar_one_or_none.return_value = entry
    session = AsyncMock()
    session.execute = AsyncMock(return_value=scalar)
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    with patch("app.db.database.AsyncSessionLocal", return_value=cm):
        result = await skip_noop_decision("recurrence:cron:abc123")
    assert result is not None
    assert result[0] == "skip_noop"


@pytest.mark.asyncio
async def test_skip_noop_actionable_outcome_none():
    entry = MagicMock()
    entry.task_id = "tid-act"
    entry.task_ended_at = datetime.utcnow() - timedelta(minutes=5)
    entry.outcome_summary = "You have 2 new messages requiring reply."
    scalar = MagicMock()
    scalar.scalar_one_or_none.return_value = entry
    session = AsyncMock()
    session.execute = AsyncMock(return_value=scalar)
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    with patch("app.db.database.AsyncSessionLocal", return_value=cm):
        result = await skip_noop_decision("recurrence:cron:abc123")
    assert result is None


@pytest.mark.asyncio
async def test_supersede_stale_failed():
    entry = MagicMock()
    entry.task_id = "tid-fail"
    entry.task_ended_at = datetime.utcnow() - timedelta(hours=2)
    entry.terminal_status = "failed"
    scalar = MagicMock()
    scalar.scalar_one_or_none.return_value = entry
    session = AsyncMock()
    session.execute = AsyncMock(return_value=scalar)
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    with patch("app.db.database.AsyncSessionLocal", return_value=cm):
        result = await supersede_decision("recurrence:cron:deadbeef")
    assert result is not None
    assert result[0] == "supersede"
    assert result[2] == "tid-fail"
