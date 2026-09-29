"""Slack identity reaches intake: message ids, replies and threads, scheduled origins."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.decisions import intake
from app.task_registry import intake_context
from app.task_registry.intake_decision_engine import apply_intake_policy

SLACK = "agent:main:slack:channel:u0aelfytlks"
DONE = "d" * 36


def test_tasks_keep_the_slack_message_identity():
    from app.api.server import TaskRequest, _slack_context

    req = TaskRequest(intent="yes", session_key=SLACK, slack_message_id="1790.0001", thread_id="1789.0009",
                      reply_to={"id": "1789.0009", "body": "Book it?"},
                      attachments=[{"path": "/m/a.pdf", "type": "application/pdf", "name": "a.pdf"}])
    assert _slack_context(req) == {"message_id": "1790.0001", "thread_id": "1789.0009", "reply_to_id": "1789.0009",
                                   "attachments": [{"path": "/m/a.pdf", "type": "application/pdf", "name": "a.pdf"}]}


def _session(scalar_values, task):
    results = []
    for value in scalar_values:
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        results.append(r)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=results)
    db.get = AsyncMock(return_value=task)
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=db)
    s.__aexit__ = AsyncMock(return_value=False)
    return lambda: s


async def test_a_slack_message_maps_to_its_task_through_messages_or_reply_parts():
    from app.task_registry import messages

    task = SimpleNamespace(id=DONE, status="completed", goal="Book the Osaka train")
    with patch.object(messages, "AsyncSessionLocal", _session([DONE], task)):
        assert await messages.task_for_slack_message("1789.0009") == {
            "task_id": DONE, "status": "completed", "goal": "Book the Osaka train"}
    with patch.object(messages, "AsyncSessionLocal", _session([None, {"task_id": DONE, "ts": "1789.0010"}], task)):
        assert (await messages.task_for_slack_message("1789.0010"))["task_id"] == DONE
    with patch.object(messages, "AsyncSessionLocal", _session([None, None], task)):
        assert await messages.task_for_slack_message("1789.0011") is None


async def test_a_reply_to_a_finished_task_puts_that_task_before_intake():
    retrieval = {"active_tasks": [], "recent_registry": [], "vector_similar": [], "evidence_pack": []}
    row = {"task_id": DONE, "terminal_status": "completed", "process_type": "user",
           "intent_snippet": "Book the Osaka train", "outcome_summary": "Asked which train", "task_ended_at": None}
    with patch.object(intake_context, "hybrid_search_bounded", AsyncMock(return_value=retrieval)), \
         patch("app.task_registry.messages.task_for_slack_message",
               AsyncMock(return_value={"task_id": DONE, "status": "completed", "goal": "Book the Osaka train"})), \
         patch.object(intake_context, "_registry_row", AsyncMock(return_value=row)), \
         patch.object(intake_context, "_load_supplementary_messages", AsyncMock(return_value={})):
        ctx = await intake_context.assemble_intake_context(
            "yes, the 10:05 one", session_key=SLACK, reply_to={"id": "1789.0009", "body": "Shall I book the 10:05 train?"})
    assert ctx["reply_to"]["task_id"] == DONE and ctx["reply_to"]["quoted"] == "Shall I book the 10:05 train?"
    assert ctx["recent_registry"][0]["task_id"] == DONE
    state, _, aliases = intake.build_intake_request({**ctx, "intent": "yes, the 10:05 one"}, [])
    assert state["replied_to"] == {"text": "Shall I book the 10:05 train?", "task": "F1"} and aliases["F1"] == DONE


def test_scheduled_runs_never_get_a_clarify_question():
    ctx = {"intent": "summarize inbox", "session_key": "agent:main:cron:job-1", "active_tasks": [], "task_type": "cron"}
    with patch("app.task_registry.intake_decision_engine.get_task_registry_intake_mode", return_value="enforce"):
        result = apply_intake_policy({"decision": "clarify", "confidence": 70}, ctx, tags=["cron"])
    assert result["decision"] == "create_fresh" and "non_interactive_no_clarify" in result["policy_overrides"]
    user_ctx = {**ctx, "session_key": SLACK, "task_type": "user"}
    with patch("app.task_registry.intake_decision_engine.get_task_registry_intake_mode", return_value="enforce"):
        assert apply_intake_policy({"decision": "clarify", "confidence": 70}, user_ctx, tags=["user-request"])["decision"] == "clarify"


def test_cron_jobs_are_read_from_the_openclaw_state_database(tmp_path, monkeypatch):
    from app.cron import reconciler

    db_path = tmp_path / "openclaw.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE cron_jobs (job_json TEXT, state_json TEXT, sort_order INTEGER)")
    con.execute("INSERT INTO cron_jobs VALUES (?, ?, 0)",
                (json.dumps({"id": "j1", "name": "Dreaming", "enabled": True, "delivery": {"mode": "none"}}),
                 json.dumps({"lastRunStatus": "ok"})))
    con.commit()
    con.close()
    monkeypatch.setattr(reconciler, "OPENCLAW_STATE_DB", str(db_path))
    assert reconciler.load_openclaw_cron_jobs() == [
        {"id": "j1", "name": "Dreaming", "enabled": True, "delivery": {"mode": "none"}, "state": {"lastRunStatus": "ok"}}]


async def test_the_intake_cache_separates_identical_texts_that_reply_to_different_messages():
    from app.activities import intake_activities

    with patch.object(intake_activities, "assemble_intake_context", AsyncMock(return_value={})):
        _, _, fp_a = await intake_activities._build_intake_context(
            {"intent": "yes", "session_key": SLACK, "reply_to": {"id": "1789.0001"}})
        _, _, fp_b = await intake_activities._build_intake_context(
            {"intent": "yes", "session_key": SLACK, "reply_to": {"id": "1789.0002"}})
    assert fp_a != fp_b
