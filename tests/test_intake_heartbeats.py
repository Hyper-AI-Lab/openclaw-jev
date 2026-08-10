"""Intake activity heartbeats during long phases."""
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_build_intake_context_emits_heartbeat():
    from app.activities.intake_activities import _build_intake_context

    beats = []

    with patch(
        "app.activities.intake_activities.assemble_intake_context",
        return_value={
            "intent": "hi",
            "active_tasks": [],
            "recent_registry": [],
            "vector_similar": [],
            "supplementary_messages": {},
        },
    ):
        with patch(
            "app.activities.openclaw_activities._safe_activity_heartbeat",
            side_effect=lambda: beats.append(1),
        ):
            ctx, _, _ = await _build_intake_context(
                {"intent": "hi", "session_key": "agent:main:main", "tags": []}
            )
    assert ctx["task_type"] == ""
    assert len(beats) == 1
