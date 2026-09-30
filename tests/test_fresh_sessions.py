"""Fresh sessions for reworks and verdicts, and readers that follow a task across all of them."""
import json
import sqlite3

import pytest

from app import openclaw_sessions as sessions
from app.activities import openclaw_activities as oc
from app.orchestrator import completion_rework, session_recovery

TID = "23c46432-dc11-450d-a160-ab180b1fdbb1"
MAIN = f"agent:main:rmp_task_{TID}"


def _msg(role, content, **extra):
    return json.dumps({"type": "message", "message": {"role": role, "content": content, **extra}})


def _write_store(tmp_path, monkeypatch, rows):
    """An OpenClaw SQLite store with the given (session_key, session_id, created_at, lines) rows."""
    db = tmp_path / "openclaw-agent.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE session_nodes (session_key TEXT PRIMARY KEY, current_session_id TEXT, "
                "entry_json TEXT, updated_at INTEGER, status TEXT, created_at INTEGER)")
    con.execute("CREATE TABLE transcript_events (session_id TEXT, seq TEXT, event_json TEXT)")
    for key, sid, created, lines in rows:
        con.execute("INSERT INTO session_nodes VALUES (?, ?, ?, ?, NULL, ?)",
                    (key, sid, json.dumps({"sessionId": sid}), created, created))
        for n, line in enumerate(lines):
            con.execute("INSERT INTO transcript_events VALUES (?, ?, ?)", (sid, str(n), line))
    con.commit()
    con.close()
    monkeypatch.setattr(sessions, "AGENT_DB_PATH", db)
    monkeypatch.setattr(sessions, "SESSIONS_JSON_PATH", tmp_path / "missing-sessions.json")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "no-jsonl")


def test_a_tasks_sessions_are_found_oldest_first_without_the_planner(tmp_path, monkeypatch):
    _write_store(tmp_path, monkeypatch, [
        (f"{MAIN}__r3", "s-r3", 300, []),
        (MAIN, "s-main", 100, []),
        (f"{MAIN}__plan", "s-plan", 50, []),
        (f"{MAIN}__r2", "s-r2", 200, []),
        (f"agent:main:rmp_verify_{TID}__v1", "s-v1", 150, []),
        ("agent:main:rmp_task_ffffffff-dc11-450d-a160-ab180b1fdbb1", "s-other", 10, []),
    ])
    assert sessions.task_session_keys(TID) == [MAIN, f"{MAIN}__r2", f"{MAIN}__r3"]
    assert sessions.task_session_keys("00000000-0000-4000-8000-000000000000") == [
        "agent:main:rmp_task_00000000-0000-4000-8000-000000000000"
    ]


def test_the_json_store_is_read_the_same_way(tmp_path, monkeypatch):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({
        MAIN: {"sessionId": "s-main", "createdAt": 100},
        f"{MAIN}__recall": {"sessionId": "s-recall", "createdAt": 400},
        f"{MAIN}__plan": {"sessionId": "s-plan", "createdAt": 50},
    }))
    monkeypatch.setattr(sessions, "SESSIONS_JSON_PATH", path)
    assert sessions.task_session_keys(TID) == [MAIN, f"{MAIN}__recall"]


def test_actions_and_page_reads_span_the_first_session_and_the_reworks(tmp_path, monkeypatch):
    page = "Osaka October climate: highs 23°C, lows 16°C, about 9 rainy days. " * 40
    _write_store(tmp_path, monkeypatch, [
        (MAIN, "s-main", 100, [
            _msg("assistant", [{"type": "toolCall", "id": "c1", "name": "web_fetch",
                                "arguments": {"url": "https://www.jma.go.jp"}}]),
            _msg("toolResult", [{"type": "text", "text": page}], toolCallId="c1"),
        ]),
        (f"{MAIN}__r2", "s-r2", 200, [
            # Tool call ids repeat across sessions; each session's are its own.
            _msg("assistant", [{"type": "toolCall", "id": "c1", "name": "read", "arguments": {"path": "/tmp/n.md"}}]),
            _msg("toolResult", "denied", toolCallId="c1", isError=True),
        ]),
    ])
    trace = sessions.task_action_trace(TID)
    assert [(t["tool"], t["ok"]) for t in trace] == [("web_fetch", True), ("read", False)]
    reads = sessions.task_tool_results(TID, tools=("web_fetch", "read"), min_chars=100)
    assert [(r["call_id"], r["tool"]) for r in reads] == [("c1", "web_fetch")]


def test_recovery_returns_the_reply_from_the_newest_session(tmp_path, monkeypatch):
    first = "Pack layers and a light rain jacket for Osaka in October, plus comfortable walking shoes."
    reworked = "Pack layers, a rain jacket and walking shoes; October in Osaka averages 23°C by day."
    _write_store(tmp_path, monkeypatch, [
        (MAIN, "s-main", 100, [_msg("assistant", [{"type": "text", "text": first}])]),
        (f"{MAIN}__r2", "s-r2", 200, [_msg("assistant", [{"type": "text", "text": reworked}])]),
    ])
    monkeypatch.setattr(session_recovery, "get_session_entry",
                        lambda key, path=None: sessions.get_session_entry(key))
    assert session_recovery.read_completed_rmp_session_reply(TID) == reworked


async def test_a_rework_is_sent_to_its_own_session_and_odd_suffixes_are_refused(monkeypatch):
    keys = []

    async def dispatch(session_key, message, **kwargs):
        keys.append(session_key)
        return "done"

    monkeypatch.setattr(oc, "_dispatch_openclaw_session", dispatch)
    monkeypatch.setattr("app.config.get_primary_agent_model", lambda: "openai/gpt-6-luna")
    await oc.send_to_openclaw({"task_id": TID, "message": "first"})
    await oc.send_to_openclaw({"task_id": TID, "message": "rework", "session_suffix": "__r2"})
    await oc.send_to_openclaw({"task_id": TID, "message": "refine", "session_suffix": "__recall"})
    assert keys == [MAIN, f"{MAIN}__r2", f"{MAIN}__recall"]
    for bad in ("__plan", "__r2; rm -rf", "_r2", "__rX"):
        with pytest.raises(ValueError):
            await oc.send_to_openclaw({"task_id": TID, "message": "x", "session_suffix": bad})


async def test_each_verdict_has_its_own_evaluator_session(monkeypatch):
    keys = []

    async def dispatch(session_key, message, **kwargs):
        keys.append(session_key)
        return "not json" if len(keys) == 1 else '{"verdict": "accept", "quality": "pass", "reason": "ok"}'

    monkeypatch.setattr(oc, "_dispatch_openclaw_session", dispatch)
    monkeypatch.setattr(oc, "_activity_deadline", lambda wrap: None)
    monkeypatch.setattr("app.llm.model_policy.drop_unwired_openai", lambda models: list(models))
    await oc._execute_on_internal_session(TID, "judge this", verdict=3)
    assert keys == [f"agent:main:rmp_verify_{TID}__v3", f"agent:main:rmp_verify_{TID}__v3_fb1"]


def test_the_rework_brief_carries_the_whole_request_draft_memory_and_actions():
    request, draft = "R" * 9000, "D" * 45000
    prompt = completion_rework.build_rework_prompt(
        request, draft, command_to_aura="Name the restaurant.", attempt=3, max_attempts=20,
        memory_block="PROCESS BRIEF:\nintake said new\n\nPROCESS-SCOPED MEMORY (use this ...):\n- fact",
        actions='1. web_fetch {"url": "x"} -> ok: page',
    )
    assert "R" * completion_rework.REQUEST_CHARS in prompt and "R" * (completion_rework.REQUEST_CHARS + 1) not in prompt
    assert "D" * completion_rework.DRAFT_CHARS in prompt
    assert prompt.index("PROCESS-SCOPED MEMORY") < prompt.index("EVALUATOR COMMAND") < prompt.index("ACTIONS ALREADY")
    assert prompt.index("ACTIONS ALREADY") < prompt.index("YOUR PRIOR RESPONSE")
    strategy = completion_rework.build_strategy_change_prompt(request, draft, memory_block="MEM", actions="ACT")
    assert strategy.startswith("STRATEGY CHANGE") and "MEM" in strategy and "ACT" in strategy
