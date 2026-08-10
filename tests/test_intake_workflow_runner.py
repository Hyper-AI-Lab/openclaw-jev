"""IntakeWorkflow runner — workflow path, no inline OpenClaw from API."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.task_registry.intake_runner import run_classify_task_intake

_PAYLOAD = {
    "intent": "hello aura",
    "session_key": "agent:main:main",
    "tags": ["user-request"],
    "recurrence_key": None,
    "task_type": "user",
}


@pytest.mark.asyncio
async def test_runner_uses_execute_workflow_not_inline_classify():
    wf_result = {
        "decision": "create_fresh",
        "effective_decision": "create_fresh",
        "execution_mode": "conversational",
        "confidence": 88,
    }
    mock_client = MagicMock()
    mock_client.execute_workflow = AsyncMock(return_value=wf_result)
    mock_inline = AsyncMock()
    mock_det = AsyncMock()

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
                result = await run_classify_task_intake(_PAYLOAD)

    assert result["execution_mode"] == "conversational"
    mock_client.execute_workflow.assert_awaited_once()
    mock_inline.assert_not_awaited()
    mock_det.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_falls_back_to_deterministic_on_workflow_failure():
    mock_det = AsyncMock(
        return_value={
            "decision": "create_fresh",
            "effective_decision": "create_fresh",
            "execution_mode": "conversational",
            "confidence": 0,
            "rationale": "Intake workflow unavailable; deterministic fallback only",
        }
    )
    mock_inline = AsyncMock()

    with patch(
        "app.task_registry.intake_runner.connect_temporal",
        side_effect=RuntimeError("no temporal"),
    ):
        with patch(
            "app.activities.intake_activities.classify_task_intake",
            mock_inline,
        ):
            with patch(
                "app.activities.intake_activities.classify_task_intake_deterministic",
                mock_det,
            ):
                result = await run_classify_task_intake(_PAYLOAD)

    mock_det.assert_awaited_once()
    mock_inline.assert_not_awaited()
    assert result["execution_mode"] == "conversational"


def test_intake_workflow_importable():
    from app.workflows.intake_workflow import IntakeWorkflow

    assert IntakeWorkflow.run is not None
