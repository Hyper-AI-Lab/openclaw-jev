"""What Claude did in a task, read back from RMP's records for memory and the evaluator."""
import json

from app.coding import direct, records
from tests.test_direct_sessions import CFG, OTHER, TASK, fakes, finished  # noqa: F401  (fakes is a fixture)


def test_a_direct_session_reads_as_a_conversation_of_what_aura_asked_and_claude_did(fakes, monkeypatch):  # noqa: F811
    monkeypatch.setattr(records, "RUNS_DIR", fakes.tmp / "runs")
    s = direct.create(TASK, "scratch", "Fix calc", CFG)
    direct.send(s["id"], "fixture:edit_and_test fix add()", CFG, plan=True, effort="high")
    finished(s["id"], 1)
    direct.end(s["id"], "task finished")
    [record] = records.task_records(TASK)
    assert record["kind"] == "session" and record["title"] == "Fix calc" and record["status"] == "ended: task finished"
    assert record["where"] == "Direct session in a scratch folder"
    [turn] = record["turns"]
    assert turn["message"] == "fixture:edit_and_test fix add()" and turn["outcome"] == "success"
    assert turn["plan"] is True and turn["effort"] == "high" and turn["models"] == ["claude-opus-5-5"]
    assert "- Turn 1 (success; planning turn; claude-opus-5-5; effort high)." in records.section_text([record])
    assert "## Turn 1 (success; planning turn; claude-opus-5-5; effort high)" in records.conversation_text(record)
    # The recorded run edited with sed through Bash, so the edit shows among the commands.
    assert any("sed -i" in command for command in turn["commands"]) and "add()" in turn["reply"]
    assert records.has_records(TASK) and not records.has_records(OTHER)


def test_a_coding_job_reads_as_rounds_with_the_brief_the_tests_and_its_pull_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(direct, "DIRECT_DIR", tmp_path / "direct")
    root = tmp_path / "runs" / TASK
    (root / "1").mkdir(parents=True)
    (root / "1" / "stream.jsonl").write_text("\n".join(json.dumps(event) for event in (
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "gh pr create --fill"}}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "Opened https://github.com/Hyper-AI-Lab/openclaw-jev/pull/7", "usage": {"output_tokens": 7}},
    )) + "\n")
    (root / "1" / "exit").write_text("success exited 0\n")
    (root / "job.json").write_text(json.dumps({"repo": "rmp", "branch": "aura/x", "created_at": "2026-10-02T04:47:30+00:00"}))
    (root / "deploy.json").write_text(json.dumps({"report": {"brief": {"title": "Status shows the commit",
                                                                       "goal": "Add the commit hash"}}}))
    (root / "verify-1").mkdir()
    (root / "verify-1" / "result.json").write_text('{"ok": true}')
    [record] = records.task_records(TASK)
    assert record["kind"] == "coding_job" and record["title"] == "Status shows the commit"
    assert record["where"] == "Coding job on rmp, branch aura/x"
    [turn] = record["turns"]
    assert turn["message"] == "Add the commit hash" and turn["tests_ok"] is True and turn["commands"] == ["gh pr create --fill"]
    assert turn["prs"] == ["https://github.com/Hyper-AI-Lab/openclaw-jev/pull/7"]
    digest = records.section_text([record])
    for words in ("Status shows the commit (Coding job on rmp, branch aura/x; 1 turn(s); recorded)",
                  "Aura asked: Add the commit hash", "RMP's tests: passed", "Pull requests: https://github.com/"):
        assert words in digest
    full = records.conversation_text(record)
    assert full.startswith("# Status shows the commit") and "## Turn 1 (success)" in full and "- gh pr create --fill" in full
    assert records.has_records(TASK)
