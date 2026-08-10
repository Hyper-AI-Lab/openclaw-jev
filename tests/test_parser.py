import pytest

from app.activities.openclaw_activities import parse_agent_evaluation


@pytest.mark.asyncio
async def test_blocked_status_preserved():
    text = 'Done.\n```json\n{"task_status": "blocked", "reason": "Need CAPTCHA"}\n```'
    result = await parse_agent_evaluation({"text": text})
    assert result["status"] == "blocked"


@pytest.mark.asyncio
async def test_needs_replan_status_preserved():
    text = '{"task_status": "needs_replan", "reason": "Site layout changed"}'
    result = await parse_agent_evaluation({"text": text})
    assert result["status"] == "needs_replan"


@pytest.mark.asyncio
async def test_invalid_status_becomes_pending():
    text = '{"task_status": "unknown_state", "reason": "x"}'
    result = await parse_agent_evaluation({"text": text})
    assert result["status"] == "pending"
