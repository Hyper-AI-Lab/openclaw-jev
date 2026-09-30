"""Shared memory holds the user's work only, and procedural memory holds procedures, not replies."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.memory.promotion import build_procedure_summary, promote_completion_memory

REPLY = "Summary sent. You must renew the passport before the trip; details at https://example.org/visa."


def test_a_reply_without_steps_or_tools_is_not_a_procedure():
    assert build_procedure_summary("Say hi", [{"name": "deliver"}], [], "Hi!") is None


def test_a_procedure_names_steps_tools_and_failures():
    trace = [{"tool": "web_search", "ok": True}, {"tool": "read", "ok": False}, {"tool": "web_search", "ok": True}]
    text = build_procedure_summary("Compare visa rules", [{"name": "gather_facts"}, {"name": "deliver"}], trace, REPLY)
    assert text.splitlines() == [
        "Task: Compare visa rules",
        "Steps: gather_facts; deliver",
        "Tools used: web_search, read (1 call(s) failed)",
        f"Result: {REPLY}",
    ]


def _db(task, run):
    db = MagicMock()
    db.get = AsyncMock(side_effect=lambda model, key: task if model.__name__ == "Task" else run)
    none = MagicMock()
    none.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=none)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=False)
    return lambda: session


async def _promote(task, run, trace):
    writes, queued = [], []

    async def write(**kwargs):
        writes.append(kwargs)
        return "m1"

    async def enqueue_facts(task_id):
        queued.append(task_id)

    with patch("app.db.database.AsyncSessionLocal", _db(task, run)), \
         patch("app.memory.router.MemoryRouter.write", side_effect=write), \
         patch("app.deep_memory.ingest.enqueue_facts", side_effect=enqueue_facts), \
         patch("app.openclaw_sessions.task_action_trace", return_value=trace):
        stats = await promote_completion_memory(process_run_id="pr", process_type=task.task_type,
                                                task_id="t1", episodic_content=REPLY)
    return stats, writes, queued


async def test_canary_work_never_becomes_shared_memory():
    canary = SimpleNamespace(goal="RMP MEMORY CANARY: Reply with one sentence", task_type="canary")
    stats, writes, queued = await _promote(canary, None, [{"tool": "read", "ok": True}])
    assert stats["skipped"] == "internal_task" and writes == [] and queued == []


async def test_user_work_queues_its_facts_and_stores_the_procedure_not_the_reply():
    task = SimpleNamespace(goal="Check visa rules for Japan", task_type="user")
    run = SimpleNamespace(plan_json={"steps": [{"name": "gather_facts"}, {"name": "deliver"}]})
    stats, writes, queued = await _promote(task, run, [{"tool": "web_search", "ok": True}])
    procedural = [w for w in writes if w["scope_type"] == "procedural"]
    assert len(procedural) == 1 and procedural[0]["content"].startswith("Task: Check visa rules for Japan")
    assert "Tools used: web_search" in procedural[0]["content"]
    assert queued == ["t1"] and stats["facts_queued"] == 1


async def test_promotion_never_writes_user_memory_itself():
    task = SimpleNamespace(goal="Remember that I prefer window seats", task_type="user")
    stats, writes, queued = await _promote(task, None, [])
    assert writes == [] and queued == ["t1"] and stats["promoted_procedural"] == 0


async def test_internal_tasks_are_not_indexed_into_the_registry():
    from app.task_registry import indexer

    summary = {"terminal_status": "completed", "intent_snippet": "RMP CANARY: Reply with exactly CANARY_OK",
               "process_type": "canary"}
    with patch("app.task_registry.summary.build_task_summary", AsyncMock(return_value=summary)), \
         patch.object(indexer, "upsert_task_vector") as upsert:
        assert await indexer.index_terminal_task("t1") is None
    upsert.assert_not_called()


def test_action_trace_pairs_calls_with_results_and_redacts(monkeypatch):
    from app import openclaw_sessions

    lines = [
        json.dumps({"type": "message", "message": {"role": "assistant", "timestamp": 10, "content": [
            {"type": "toolCall", "id": "c1", "name": "read", "arguments": {"path": "/tmp/a"}},
            {"type": "toolCall", "id": "c2", "name": "exec", "arguments": {"cmd": "curl -H 'Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz0123'"}}]}}),
        json.dumps({"type": "message", "message": {"role": "toolResult", "toolCallId": "c1", "isError": False,
                                                    "content": [{"type": "text", "text": "file body"}]}}),
        json.dumps({"type": "message", "message": {"role": "toolResult", "toolCallId": "c2", "isError": True,
                                                    "content": "denied"}}),
    ]
    monkeypatch.setattr(openclaw_sessions, "get_session_entry", lambda key: {"sessionId": "s1"})
    monkeypatch.setattr(openclaw_sessions, "read_transcript_lines", lambda sid: lines)
    trace = openclaw_sessions.task_action_trace("t1")
    assert [(t["tool"], t["ok"], t["result"]) for t in trace] == [("read", True, "file body"), ("exec", False, "denied")]
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in trace[1]["arguments"]
    assert openclaw_sessions.task_action_trace("t1", since_ms=11) == []
