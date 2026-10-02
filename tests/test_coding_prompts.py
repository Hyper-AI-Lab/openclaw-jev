"""What Aura and Claude Code are told in a coding task, how their answers are read, and what a deploy restarts."""
import pytest

from app.coding import prompts
from app.coding.deploy import restarts_for

REPOS = {"rmp": {"remote": "Hyper-AI-Lab/openclaw-jev", "deploy": "self"},
         "agentic-design": {"remote": "Hyper-AI-Lab/agentic-design", "deploy": "pr"}}


def test_a_brief_names_its_repo_by_name_or_by_github_remote():
    by_name = prompts.parse_brief('```json\n{"repo": "RMP", "title": "Fix it", "goal": "Say hello."}\n```', REPOS)
    by_remote = prompts.parse_brief('{"repo": "Hyper-AI-Lab/agentic-design", "goal": "Add a button.", '
                                    '"acceptance_criteria": "the button shows", "constraints": []}', REPOS)
    assert by_name["repo"] == "rmp" and by_name["questions"] == [] and by_name["title"] == "Fix it"
    assert by_remote["repo"] == "agentic-design" and by_remote["acceptance_criteria"] == ["the button shows"]
    assert by_remote["title"] == "Add a button.", "a missing title comes from the goal"


def test_a_brief_without_a_known_repo_asks_kirill_which_one():
    brief = prompts.parse_brief('{"repo": "website", "goal": "Change the footer."}', REPOS)
    assert brief["repo"] is None and brief["questions"] == ["Which repository should I change: rmp, agentic-design?"]


@pytest.mark.parametrize("text", ["I'll ask Claude to do it.", '{"repo": "rmp", "goal": ""}', "[1, 2]", ""])
def test_an_unusable_brief_is_an_error(text):
    with pytest.raises(ValueError):
        prompts.parse_brief(text, REPOS)


@pytest.mark.parametrize("text, verdict, readable", [
    ('{"verdict": "ready", "feedback": [], "reply": "Done, tests pass."}', "ready", True),
    ('{"verdict": "Rework", "feedback": "Add a test.", "reply": "Needs a test."}', "rework", True),
    ('{"verdict": "ready", "reply": ""}', "rework", False),
    ('{"verdict": "ship it", "reply": "Looks fine."}', "rework", False),
    ("Looks good to me!", "rework", False),
])
def test_a_review_that_cannot_be_read_is_a_rework_never_a_pass(text, verdict, readable):
    review = prompts.parse_review(text)
    assert review["verdict"] == verdict and ("error" not in review) == readable


def test_shipping_is_never_part_of_the_brief_or_held_against_a_round():
    brief = prompts.brief_prompt("Fix the README and open a PR.", REPOS, answers=[])
    review = prompts.review_prompt({"goal": "Fix it.", "acceptance_criteria": [], "constraints": []}, 1,
                                   {"kind": "success"}, {"collected": {}, "tests": None}, "")
    assert "never put pushing, a pull request or a deploy" in brief and "RMP opens the pull request" in brief
    assert "never ask\nClaude Code for them" in review and "never hold the round back" in review


def test_a_rework_carries_the_feedback_and_only_the_failing_tests():
    tests = {"ok": False, "commands": [
        {"command": ["npm", "ci"], "setup": True, "ok": True, "exit": "success exited 0", "tail": "added 10 packages"},
        {"command": ["npm", "test"], "setup": False, "ok": False, "exit": "exit-code exited 1",
         "tail": "\n".join(f"line {i}" for i in range(100))}]}
    prompt = prompts.rework_prompt(["Rename the flag."], tests)
    assert "- Rename the flag." in prompt and "$ npm test\n(exit-code exited 1)" in prompt
    assert "line 99" in prompt and "line 59" not in prompt and "added 10 packages" not in prompt


def test_claude_is_told_the_repo_branch_and_test_commands():
    text = prompts.system_prompt(REPOS["rmp"], "aura/t1-x", [["/srv/aura-code/venvs/rmp/bin/python", "-m", "pytest", "-q"]])
    assert "Hyper-AI-Lab/openclaw-jev on branch aura/t1-x" in text and "/srv/aura-code/venvs/rmp/bin/python -m pytest -q" in text
    assert "Do not push" in text


def test_the_review_shows_rmps_evidence_and_cuts_a_long_diff():
    evidence = {"collected": {"commits": [{"sha": "c" * 40, "subject": "Fix"}], "diffstat": " a.py | 1 +",
                              "dependencies_changed": ["requirements.txt"], "truncated": False,
                              "secrets": [{"kind": "GitHub token", "path": "a.py", "diff_line": 3}]},
                "tests": {"ok": True, "commands": [{"command": ["pytest"], "ok": True, "counts": {"passed": 3}}]}}
    text = prompts.review_prompt({"goal": "Fix it.", "acceptance_criteria": [], "constraints": []}, 2,
                                 {"kind": "max_turns", "report": None}, evidence, "x" * (prompts.DIFF_CHARS_FOR_REVIEW + 5))
    assert "round 2 of the coding task (it ran out of turns before finishing)" in text
    assert "- cccccccccc Fix" in text and "RMP's own test run: passed (3 passed)" in text
    assert "Dependency files changed: requirements.txt" in text and "Secret scan: GitHub token in a.py" in text
    assert "DIFF (cut short):" in text


@pytest.mark.parametrize("paths, services", [
    (["app/workflows/coding_task.py"], ["rmp-api", "rmp-worker"]),
    (["worker.py", "requirements.txt"], ["rmp-api", "rmp-worker"]),
    (["plugins/aura_web/index.js"], ["openclaw-gateway"]),
    (["web-stack/server.py", "docs/CONCEPT_TREE.md"], ["aura-web-backends"]),
    (["systemd/aura-coder-firewall.service", "systemd/rmp-janitor.timer"], ["aura-coder-firewall", "rmp-janitor.timer"]),
    (["docs/x.md", "tests/test_x.py", "ops/x.sh", ".cursor/rules/rmp-architecture.mdc", "application.md"], []),
])
def test_a_deploy_restarts_only_what_the_change_touches(paths, services):
    assert restarts_for(paths) == services
