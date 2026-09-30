"""The Process Evaluator sees what Aura did, not only what she said."""
from unittest.mock import AsyncMock, patch

from app.orchestrator.process_evaluator import build_evaluator_prompt, format_action_trace, format_artifacts

TRACE = [
    {"tool": "web_search", "arguments": '{"query": "Osaka weather October"}', "ok": True, "result": "18-24C, some rain"},
    {"tool": "write", "arguments": '{"path": "/tmp/plan.md"}', "ok": False, "result": "permission denied"},
]


def test_the_trace_lists_each_call_with_its_outcome():
    text = format_action_trace(TRACE)
    assert text.splitlines() == [
        '1. web_search {"query": "Osaka weather October"} -> ok: 18-24C, some rain',
        '2. write {"path": "/tmp/plan.md"} -> FAILED: permission denied',
    ]
    assert "no tool calls" in format_action_trace([])
    assert format_artifacts([{"kind": "completion_output", "filename": "plan.txt"}]) == "- completion_output: plan.txt"


def test_the_prompt_asks_to_check_claims_against_the_trace():
    prompt = build_evaluator_prompt({"user_intent": "save a packing plan", "agent_response": "I saved the plan.",
                                     "tools_taken": format_action_trace(TRACE), "artifacts": "(none recorded)"})
    assert "write {\"path\": \"/tmp/plan.md\"} -> FAILED" in prompt
    assert "A claim with no matching successful action is not done" in prompt
    assert "(not provided)" not in prompt.split("SITUATIONAL TOOLS")[0]


async def test_verify_sends_the_trace_and_artifacts_to_the_evaluator():
    from app.activities import openclaw_activities as oa

    seen = {}

    async def execute(task_id, prompt, verdict=0):
        seen["prompt"], seen["verdict"] = prompt, verdict
        return '{"verdict": "accept", "quality": "pass", "reason": "ok"}'

    with patch("app.openclaw_sessions.task_action_trace", return_value=TRACE), \
         patch("app.artifacts.store.ArtifactStore.list_for_process",
               AsyncMock(return_value=[{"kind": "completion_output", "filename": "plan.txt"}])), \
         patch("app.orchestrator.evaluator_tools.collect_situational_context", AsyncMock(return_value="(skipped)")), \
         patch("app.orchestrator.process_evaluator.persist_evaluator_verdict", AsyncMock()), \
         patch.object(oa, "_execute_on_internal_session", side_effect=execute):
        result = await oa.verify_response_quality(
            {"task_id": "t1", "user_intent": "save a packing plan", "agent_response": "I saved the plan.",
             "process_run_id": "pr1", "attempt": 1})
    assert result["verdict"] == "accept" and seen["verdict"] == 1
    assert "2. write" in seen["prompt"] and "- completion_output: plan.txt" in seen["prompt"]


def test_a_reply_to_the_previous_turn_never_answers_this_dispatch():
    import json

    from app.activities.openclaw_activities import _poll_jsonl_for_response

    def msg(role, text, ts, stop=None):
        m = {"role": role, "content": [{"type": "text", "text": text}], "timestamp": ts}
        if stop:
            m["stopReason"] = stop
        return json.dumps({"type": "message", "timestamp": ts, "message": m})

    earlier = [msg("user", "[cron:a Hook] [RMP_DISPATCH first] judge draft 1", 1000),
               msg("assistant", '{"verdict": "accept", "quality": "pass"}', 2000, "stop")]
    marker = "[RMP_DISPATCH second]"
    # The previous verdict is inside the 5 s window before this dispatch started.
    text, _, _ = _poll_jsonl_for_response("", 0.0, earlier, marker)
    assert text == ""
    lines = earlier + [msg("user", f"[cron:b Hook] {marker} judge draft 2", 3000),
                       msg("assistant", '{"verdict": "rework", "quality": "fail"}', 4000, "stop")]
    text, reason, _ = _poll_jsonl_for_response("", 0.0, lines, marker)
    assert '"rework"' in text and reason == "stop"
