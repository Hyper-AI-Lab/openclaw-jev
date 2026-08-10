"""Deterministic intake fallback — no OpenClaw from API."""
from unittest.mock import AsyncMock, patch

import pytest

from app.activities.intake_activities import classify_task_intake_deterministic


@pytest.mark.asyncio
async def test_deterministic_intake_never_calls_openclaw():
    payload = {
        "intent": "hello aura",
        "session_key": "agent:main:main",
        "tags": ["user-request"],
        "task_type": "user",
    }
    mock_openclaw = AsyncMock(side_effect=AssertionError("OpenClaw must not run in API fallback"))

    with patch(
        "app.activities.intake_activities._build_intake_context",
        new=AsyncMock(
            return_value=(
                {"intent": payload["intent"], "active_tasks": [], "task_type": "user", "session_key": payload["session_key"]},
                None,
                "fp123",
            )
        ),
    ):
        with patch(
            "app.activities.intake_activities.run_intake_deterministic_gates",
            new=AsyncMock(return_value=None),
        ):
            with patch(
                "app.activities.openclaw_activities._execute_on_internal_session",
                mock_openclaw,
            ):
                result = await classify_task_intake_deterministic(payload)

    mock_openclaw.assert_not_awaited()
    assert result["execution_mode"] == "conversational"
    assert result["confidence"] == 0
