"""Create task intake integration (mocked LLM)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_create_task_uses_intake_runner():
    from app.task_registry.intake_runner import run_classify_task_intake

    payload = {
        "intent": "hello",
        "session_key": "agent:main:main",
        "tags": ["user-request"],
        "recurrence_key": None,
        "task_type": "user",
    }
    mock_client = MagicMock()
    mock_client.execute_workflow = AsyncMock(
        return_value={
            "decision": "create_fresh",
            "effective_decision": "create_fresh",
            "intake_mode": "enforce",
            "confidence": 80,
            "request_hash": "x",
        }
    )
    mock_det = AsyncMock()
    mock_inline = AsyncMock()
    with patch(
        "app.task_registry.intake_runner.connect_temporal",
        new=AsyncMock(return_value=mock_client),
    ):
        with patch(
            "app.activities.intake_activities.classify_task_intake",
            mock_inline,
        ):
            with patch(
                "app.activities.intake_activities.classify_task_intake_deterministic",
                mock_det,
            ):
                result = await run_classify_task_intake(payload)
    assert result["effective_decision"] == "create_fresh"
    mock_client.execute_workflow.assert_awaited_once()
    mock_inline.assert_not_awaited()
    mock_det.assert_not_awaited()
