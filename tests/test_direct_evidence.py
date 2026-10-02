"""What the evaluator sees of Aura's own work with Claude: her sessions and the pull requests RMP merged."""
from datetime import datetime, timedelta

import pytest

from app.activities import openclaw_activities as oa
from app.db.models import Event
from tests.test_invariants import seed, session, task  # noqa: F401  (session is a fixture)

TASK = "11111111-2222-4333-8444-555555555555"
TOKEN = "sk-ant-oat01-" + "Ab3_-" * 19
T0 = datetime(2026, 10, 2, 12, 0)


def record(kind, title):
    return {"kind": kind, "id": title, "title": title, "where": "Direct session in a clone of her repository",
            "status": "ended: task finished", "started_at": "2026-10-02T12:00:00+00:00",
            "turns": [{"number": 1, "message": "Fix the greeting", "outcome": "success",
                       "reply": f"Fixed it; tests pass. The env had {TOKEN}.", "files_edited": ["app/greet.py"],
                       "commands": ["pytest -q"], "prs": ["https://github.com/Hyper-AI-Lab/openclaw-jev/pull/12"],
                       "tokens": 100}]}


def event(event_type, minutes, **payload):
    return Event(correlation_id=TASK, entity_type="task", entity_id=TASK, event_type=event_type,
                 event_payload=payload, occurred_at=T0 + timedelta(minutes=minutes))


@pytest.fixture
def records(monkeypatch):
    found = []
    monkeypatch.setattr("app.coding.records.task_records", lambda task_id: list(found))
    return found


async def test_the_evaluator_sees_her_sessions_the_merge_and_the_deploy_redacted(session, records, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.db.database.AsyncSessionLocal", session)
    records += [record("session", "Greeting fix"), record("coding_job", "A reviewed job")]
    await seed(session, task(TASK, status="running"),
               event("coding.pr_merged", 1, pr=12, url="https://github.com/Hyper-AI-Lab/openclaw-jev/pull/12", merge="m" * 40),
               event("coding.deploy", 2, status="deployed", summary="Deployed mmmmmmmmmmmm to main."))
    text = await oa._direct_claude_evidence(TASK)
    assert text.startswith("Aura's Claude sessions (what she asked, what Claude did and answered):\nGreeting fix")
    assert "A reviewed job" not in text and "Aura asked: Fix the greeting" in text and "Files edited: app/greet.py" in text
    assert "RMP merged PR #12 (https://github.com/Hyper-AI-Lab/openclaw-jev/pull/12) after CI's test check passed, as mmmmmmmmmmmm." in text
    assert "Deploy deployed: Deployed mmmmmmmmmmmm to main." in text
    assert TOKEN[:12] not in text and "[REDACTED:api_key]" in text


async def test_a_reviewed_jobs_deploys_are_its_own_evidence_and_nothing_means_nothing(session, records, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.db.database.AsyncSessionLocal", session)
    await seed(session, task(TASK, status="running"), event("coding.deploy", 2, status="deployed", summary="Deployed."))
    assert await oa._direct_claude_evidence(TASK) == ""
    await seed(session, event("coding.pr_not_merged", 3, pr=13, summary="CI's test check on PR #13 is failure; ..."))
    assert await oa._direct_claude_evidence(TASK) == (
        "Deploy deployed: Deployed.\nRMP did not merge PR #13: CI's test check on PR #13 is failure; ...")
