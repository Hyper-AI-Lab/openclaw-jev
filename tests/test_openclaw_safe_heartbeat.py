"""OpenClaw activity helpers — safe heartbeat outside worker context."""
from unittest.mock import patch

from app.activities.openclaw_activities import _safe_activity_heartbeat


def test_safe_activity_heartbeat_noop_outside_activity_context():
    with patch(
        "app.activities.openclaw_activities.activity.heartbeat",
        side_effect=RuntimeError("Not in activity context"),
    ):
        _safe_activity_heartbeat()
