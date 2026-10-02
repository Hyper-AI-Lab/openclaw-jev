"""What the evaluator sees of Aura's own work with Claude: her sessions, and what GitHub says of their pull requests."""
import pytest

from app.activities import openclaw_activities as oa
from app.coding import github

TASK = "11111111-2222-4333-8444-555555555555"
TOKEN = "sk-ant-oat01-" + "Ab3_-" * 19
PR = "https://github.com/Hyper-AI-Lab/openclaw-jev/pull/12"


def record(kind, title, prs=(PR,)):
    return {"kind": kind, "id": title, "title": title, "where": "Direct session in a clone of her repository",
            "status": "ended: task finished", "started_at": "2026-10-02T12:00:00+00:00",
            "turns": [{"number": 1, "message": "Fix the greeting", "outcome": "success",
                       "reply": f"Fixed it; tests pass. The env had {TOKEN}.", "files_edited": ["app/greet.py"],
                       "commands": ["pytest -q"], "prs": list(prs), "tokens": 100}]}


@pytest.fixture
def records(monkeypatch):
    found = []
    monkeypatch.setattr("app.coding.records.task_records", lambda task_id: list(found))
    monkeypatch.setattr("app.config.get_coding_config", lambda: {"repositories": {"rmp": {"remote": "Hyper-AI-Lab/openclaw-jev"}}})
    return found


async def test_the_evaluator_sees_her_sessions_and_githubs_word_on_their_pull_requests(records, monkeypatch):
    records += [record("session", "Greeting fix", prs=(PR, "https://github.com/someone/else/pull/3")),
                record("coding_job", "A reviewed job")]
    monkeypatch.setattr(github, "pull", lambda number, repo: {"merged": True, "state": "closed", "head": {"sha": "h" * 40}})
    monkeypatch.setattr(github, "check", lambda sha, repo: "success")
    text = await oa._direct_claude_evidence(TASK)
    assert text.startswith("Aura's Claude sessions (what she asked, what Claude did and answered):\nGreeting fix")
    assert "A reviewed job" not in text and "Aura asked: Fix the greeting" in text and "Files edited: app/greet.py" in text
    assert f"GitHub: PR #12 ({PR}) is merged; CI's test check on it: success." in text
    assert "someone/else" not in text.split("GitHub:", 1)[1]
    assert TOKEN[:12] not in text and "[REDACTED:api_key]" in text


async def test_github_out_of_reach_is_said_and_no_session_means_no_evidence(records, monkeypatch):
    assert await oa._direct_claude_evidence(TASK) == ""

    def down(number, repo):
        raise github.GitHubError("GET pulls/12: connection refused")

    records.append(record("session", "Greeting fix"))
    monkeypatch.setattr(github, "pull", down)
    assert "GitHub: PR #12 could not be checked (GET pulls/12: connection refused)." in await oa._direct_claude_evidence(TASK)
