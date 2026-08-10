"""OpenClaw 2026.7 Slack origin → user id resolution."""
from __future__ import annotations

import json

from app.activities import openclaw_activities as oc


def test_extract_slack_user_id_from_channel_origin():
    assert oc._extract_slack_user_id("slack:channel:U0AELFYTLKS") == "U0AELFYTLKS"
    assert oc._extract_slack_user_id("channel:U0AELFYTLKS") == "U0AELFYTLKS"
    assert oc._extract_slack_user_id("slack:U0AELFYTLKS") == "U0AELFYTLKS"
    assert oc._extract_slack_user_id("user:U0AELFYTLKS") == "U0AELFYTLKS"
    assert oc._extract_slack_user_id("slack:channel:D0ADY6N3HPY") == ""


def test_get_slack_user_id_from_slack_session(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions.json"
    sessions.write_text(
        json.dumps(
            {
                "agent:main:slack:channel:u0aelfytlks": {
                    "origin": {
                        "from": "slack:channel:U0AELFYTLKS",
                        "to": "channel:U0AELFYTLKS",
                    }
                }
            }
        )
    )
    monkeypatch.setattr(oc, "SESSIONS_JSON_PATH", str(sessions))
    assert (
        oc._get_slack_user_id("agent:main:slack:channel:u0aelfytlks")
        == "U0AELFYTLKS"
    )


def test_get_slack_user_id_fallback_scan_and_owner(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions.json"
    sessions.write_text(
        json.dumps(
            {
                "agent:main:slack:channel:u0aelfytlks": {
                    "origin": {"from": "slack:channel:U0AELFYTLKS"}
                }
            }
        )
    )
    monkeypatch.setattr(oc, "SESSIONS_JSON_PATH", str(sessions))
    # Historical task session key missing from store — scan slack entries.
    assert oc._get_slack_user_id("agent:main:main") == "U0AELFYTLKS"

    sessions.write_text("{}")
    monkeypatch.setattr(
        "app.config.get_slack_owner_user_id", lambda: "U0AELFYTLKS"
    )
    assert oc._get_slack_user_id("agent:main:main") == "U0AELFYTLKS"
