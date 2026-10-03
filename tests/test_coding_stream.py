"""Claude Code stream-json parsing on runs recorded on this host (tests/fixtures/claude_streams)."""
import json
from pathlib import Path

import pytest

from app.coding import stream

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_streams"


def lines_of(name):
    # "\n" alone, as the runner reads: str.splitlines also splits at U+2028, which JSON strings may carry raw.
    return (FIXTURES / f"{name}.jsonl").read_text().split("\n")


def recorded(name):
    return stream.parse_lines(lines_of(name)), (FIXTURES / f"{name}.exit").read_text().strip()


@pytest.mark.parametrize("name, kind", [
    ("success_readonly", "success"), ("edit_and_test", "success"), ("max_turns", "max_turns"),
    ("auth_failure", "auth_failed"), ("unknown_model", "api_error"), ("interrupted", "no_result"),
    ("synthetic_usage_limit", "usage_limit"), ("resume_first", "success"), ("resumed", "success"),
])
def test_every_recorded_run_parses_cleanly_and_ends_as_it_did(name, kind):
    state, exit_line = recorded(name)
    assert state.malformed == 0 and state.events > 0
    assert state.session_id and state.model and state.claude_code_version == "2.1.280"
    assert stream.outcome(state, exit_line=exit_line).kind == kind


def test_a_successful_run_carries_its_structured_report_turns_and_cost():
    state, exit_line = recorded("success_readonly")
    result = stream.outcome(state, exit_line=exit_line)
    assert result.report["phrase"] == "HERON-7" and result.num_turns == state.result["num_turns"]
    assert result.cost_usd == state.result["total_cost_usd"] and result.error is None
    assert state.files_read and state.files_read[0].endswith("README.md")
    assert "StructuredOutput" not in state.tool_counts and "Structured report ready" in state.milestones
    assert state.permission_mode == "bypassPermissions"


def test_commands_run_and_their_milestones_are_collected():
    state, _ = recorded("edit_and_test")
    assert state.tool_counts == {"Bash": 2} and len(state.commands) == 2
    assert any("python3 -m unittest -q" in c for c in state.commands)
    # The fix went through sed: tool uses alone cannot tell which files changed.
    assert state.files_edited == []
    assert sum(m.startswith("Ran: ") for m in state.milestones) == 2 and state.tool_errors == 0


def test_an_api_error_ends_in_a_success_result_and_is_still_a_failure():
    auth, exit_line = recorded("auth_failure")
    assert auth.result["subtype"] == "success" and auth.result["is_error"]
    result = stream.outcome(auth, exit_line=exit_line)
    assert result.error.startswith("Failed to authenticate")
    assert [r["error"] for r in auth.api_retries] == ["authentication_failed"] * len(auth.api_retries)
    assert auth.assistant_errors == ["authentication_failed"]
    model, exit_line = recorded("unknown_model")
    assert model.assistant_errors == ["model_not_found"] and "selected model" in stream.outcome(model, exit_line=exit_line).error


def test_a_stopped_run_has_no_result_and_reads_as_stopped_only_when_rmp_stopped_it():
    state, exit_line = recorded("interrupted")
    assert state.result is None and set(state.background_tasks.values()) == {"stopped"}
    assert stream.outcome(state, exit_line=exit_line).kind == "no_result"
    assert stream.outcome(state, exit_line=exit_line, stopped=True).kind == "stopped"
    assert stream.outcome(state).kind == "running"


def test_a_usage_limit_carries_its_reset_time():
    state, exit_line = recorded("synthetic_usage_limit")
    result = stream.outcome(state, exit_line=exit_line)
    assert result.resets_at == 1790848800 and state.rate_limit["type"] == "five_hour"
    assert any(m.startswith("Rate limit rejected, resets 2026-") for m in state.milestones)


def test_a_reset_time_in_milliseconds_is_read_as_seconds():
    state = stream.parse_lines([json.dumps({"type": "rate_limit_event",
                                            "rate_limit_info": {"status": "allowed_warning", "resetsAt": 1790848800123}})])
    assert state.rate_limit["resets_at"] == 1790848800 and state.milestones[-1].startswith("Rate limit warning")
    stream.feed(state, json.dumps({"type": "rate_limit_event", "rate_limit_info": {"resetsAt": "soon"}}))
    assert state.rate_limit["resets_at"] == 1790848800 and state.malformed == 1


def test_a_resumed_run_continues_the_same_session():
    first, _ = recorded("resume_first")
    second, _ = recorded("resumed")
    assert first.session_id == second.session_id and second.result["result"] == "OSPREY-4"


def test_usage_adds_up_per_run_and_per_model():
    state, _ = recorded("success_readonly")
    usage = stream.usage_summary(state.result)
    parts = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
    assert usage["total_tokens"] == sum(usage[p] for p in parts) > 0
    assert set(usage["models"]) == {"claude-opus-5-5"} and usage["cost_usd"] == state.result["total_cost_usd"]
    assert stream.usage_summary(None) == {**{p: 0 for p in parts}, "total_tokens": 0, "cost_usd": 0.0, "models": {}}


def test_feeding_line_by_line_matches_parsing_the_whole_stream():
    lines = lines_of("edit_and_test")
    state = stream.StreamState()
    for line in lines:
        stream.feed(state, line)
    assert state == stream.parse_lines(lines)


def test_blank_truncated_and_foreign_lines_never_raise():
    state = stream.parse_lines(["", "   ", '{"type": "assist', "not json", "[1, 2]", '{"type": "unknown_kind"}'])
    assert (state.events, state.malformed) == (1, 3)
    assert stream.outcome(stream.StreamState()).kind == "running"


def test_the_models_are_those_claudes_messages_came_from_not_the_one_init_names():
    lines = [json.dumps(event) for event in (
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"},
        {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "Plan"}]}},
        {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "More"}]}},
        {"type": "assistant", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API error"}]}},
    )]
    state = stream.parse_lines(lines)
    assert state.model == "claude-sonnet-5-5" and state.models == ["claude-opus-5-5"]
