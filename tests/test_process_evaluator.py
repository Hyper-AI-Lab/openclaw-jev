"""Process Evaluator parse + always-gated Slack contract."""
from app.orchestrator.decision_engine import decide_completion_gate
from app.orchestrator.process_evaluator import (
    build_evaluator_prompt,
    parse_evaluator_response,
)
from app.workflows import generic_task
import inspect


def test_malformed_judge_json_is_rework_not_accept():
    result = parse_evaluator_response("not json at all")
    assert result["verdict"] == "rework"
    assert result["quality"] == "fail"
    assert result["parse_error"] is True
    assert "evaluator error" in result["issues"]


def test_judge_error_prefix_fail_closed():
    result = parse_evaluator_response("Error: OpenClaw timeout")
    assert result["quality"] == "fail"
    assert result["verdict"] == "rework"


def test_accept_verdict_maps_quality_pass():
    result = parse_evaluator_response(
        '{"verdict": "accept", "quality": "fail", "reason": "greeting ok"}'
    )
    assert result["verdict"] == "accept"
    assert result["quality"] == "pass"


def test_quality_fail_without_verdict_is_rework():
    result = parse_evaluator_response(
        '{"quality": "fail", "issues": "missing the file read"}'
    )
    assert result["verdict"] == "rework"
    assert result["quality"] == "fail"
    assert "missing the file read" in result["issues"]


def test_evaluator_prompt_is_not_aura():
    prompt = build_evaluator_prompt(
        {
            "user_intent": "hello",
            "agent_response": "Hi Kirill",
            "attempt": 1,
            "process_brief": "PROCESS BRIEF: new work",
        }
    )
    assert "PROCESS EVALUATOR" in prompt
    assert "not Aura" in prompt
    assert "PROCESS BRIEF: new work" in prompt
    assert "hello" in prompt


EVIDENCE = {
    "claude": {"outcome": "success", "num_turns": 7, "commands": ["python3 -m pytest -q tests/test_app.py"],
               "files_edited": ["app/coding/runner.py"]},
    "tests": {"ok": False, "commands": [{"command": ["python", "-m", "pytest", "-q"], "exit": "exit-code exited 1",
                                         "counts": {"failed": 2, "passed": 879}}]},
    "commits": [{"sha": "5e11a266b598aa", "subject": "Fix the runner", "author": "Aura (Claude Code)"}],
    "diffstat": " app/coding/runner.py | 4 ++--\n 1 file changed, 2 insertions(+), 2 deletions(-)",
    "secrets": [{"path": "leaked.py", "kind": "GitHub token"}],
}


def test_code_claims_are_judged_against_the_evidence_rmp_recorded():
    from app.orchestrator.process_evaluator import format_external_evidence

    text = format_external_evidence(EVIDENCE)
    assert "Claude Code run: success, 7 turns" in text and "- ran: python3 -m pytest -q tests/test_app.py" in text
    assert "- edited: app/coding/runner.py" in text and "RMP's own test run: FAILED" in text
    assert "exit-code exited 1 {'failed': 2, 'passed': 879}" in text
    assert "5e11a266b5 Fix the runner (Aura (Claude Code))" in text and "1 file changed" in text
    assert "Secret scan: 1 finding(s)" in text
    prompt = build_evaluator_prompt({"user_intent": "fix the runner", "agent_response": "Fixed; all tests pass.",
                                     "attempt": 1, "external_evidence_text": text})
    assert "EXTERNAL EVIDENCE (recorded by RMP, not by Aura):\nClaude Code run" in prompt
    assert "tests pass only when RMP's own test run passed, or GitHub reports CI's test check passed on the pull request" in prompt


def test_the_evaluator_sees_a_bounded_copy_of_the_diff(tmp_path):
    from app.orchestrator.process_evaluator import DIFF_CHARS, format_external_evidence

    diff = tmp_path / "diff-1.patch"
    diff.write_text("+## Testing\n+Run `npm test`.\n")
    assert "Diff (RMP's copy):\n+## Testing\n+Run `npm test`." in format_external_evidence({**EVIDENCE, "diff_file": str(diff)})
    diff.write_text("+x\n" * DIFF_CHARS)
    assert "Diff (RMP's copy, cut short):" in format_external_evidence({"diff_file": str(diff)})
    assert "Diff (RMP's copy" not in format_external_evidence({**EVIDENCE, "diff_file": str(tmp_path / "missing.patch")})


def test_a_final_coding_reply_is_judged_against_what_shipped():
    from app.orchestrator.process_evaluator import format_external_evidence

    text = format_external_evidence({**EVIDENCE, "deploy": {"status": "deployed",
                                                            "summary": "Deployed 5e11a266b598 to main; canary passed."}})
    assert text.endswith("After Kirill's approval: deployed. Deployed 5e11a266b598 to main; canary passed.")


def test_without_evidence_the_section_says_none():
    from app.orchestrator.process_evaluator import format_external_evidence

    assert format_external_evidence(None) == "" and format_external_evidence({}) == ""
    prompt = build_evaluator_prompt({"user_intent": "hello", "agent_response": "Hi Kirill", "attempt": 1})
    assert "EXTERNAL EVIDENCE (recorded by RMP, not by Aura):\n(none)" in prompt


def test_missing_quality_without_skip_retries():
    d = decide_completion_gate(
        evidence_passed=True,
        evidence_issues=[],
        quality_passed=None,
        skip_quality_llm=False,
    )
    assert d["action"] == "retry"


def test_conversational_no_longer_bypasses_quality_gate():
    plan = inspect.getsource(generic_task.GenericTaskWorkflow._plan_driven_loop)
    assert "if is_conversational and clean_result.strip():" not in plan
    assert "notify_slack_user" not in plan
    assert "return await self._judge_and_deliver(" in plan
    source = inspect.getsource(generic_task.GenericTaskWorkflow._judge_and_deliver)
    assert "await self._judge(" in source
    assert "internal = is_internal_task(user_intent, task_type, tags)" in source
    verify_pos = source.find("await self._judge(")
    notify_complete = source.find("await self._deliver_final(", verify_pos)
    assert notify_complete != -1
    assert verify_pos < notify_complete


def test_conversational_still_defers_mid_step_memory():
    source = inspect.getsource(generic_task.GenericTaskWorkflow._plan_driven_loop)
    assert "if not is_conversational:" in source


def test_catalog_evidence_failure_enters_rework_not_instant_fail():
    from app.workflows.catalog_task import CatalogTaskWorkflow

    src = inspect.getsource(CatalogTaskWorkflow._run)
    assert "Completion evidence missing" not in src
    assert "Artifact evidence failed" not in src
    ev_idx = src.find("catalog_evidence = check_catalog_completion")
    fail_idx = src.find("finalize_task_failure", ev_idx)
    rework_idx = src.find("build_rework_prompt", ev_idx)
    assert ev_idx != -1
    assert rework_idx != -1
    assert rework_idx < fail_idx


async def test_evaluator_results_decode_the_way_the_workflow_receives_them():
    import typing

    from temporalio.converter import DataConverter

    from app.activities.openclaw_activities import verify_response_quality

    # The workflow decodes the activity result with this annotation.
    return_type = typing.get_type_hints(verify_response_quality)["return"]
    for raw in ('{"verdict": "accept", "quality": "pass", "reason": "ok"}', "not json at all"):
        result = parse_evaluator_response(raw)
        payloads = await DataConverter.default.encode([result])
        assert await DataConverter.default.decode(payloads, [return_type]) == [result]
