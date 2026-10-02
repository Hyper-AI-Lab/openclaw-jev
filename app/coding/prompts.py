"""What Aura and Claude Code are told in a coding task, and how their answers are read.

Aura writes the brief and reviews every round; Claude Code gets the brief as its task and, on rework,
the review's feedback and RMP's failing tests. The parsers fail closed: output that cannot be read
is an error or a rework, never a pass.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.orchestrator.process_evaluator import _extract_json_object

DIFF_CHARS_FOR_REVIEW = 60_000
TEST_TAIL_LINES = 40

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests_run": {"type": "array", "items": {"type": "string"}},
        "tests_passed": {"type": "boolean"},
        "open_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "tests_passed"],
}

CONTINUE_PROMPT = ("You were interrupted by the usage limit. Continue the task from where you stopped, "
                   "then commit and finish with the structured report.")


def _json(text: str) -> Optional[Dict[str, Any]]:
    blob = _extract_json_object(text or "")
    try:
        loaded = json.loads(blob) if blob else None
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    return [str(item).strip() for item in value or [] if str(item).strip()] if isinstance(value, list) else []


def _bullets(items: List[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or "- (none)"


def repo_lines(repositories: Dict[str, Dict[str, Any]]) -> str:
    return "\n".join(
        f"- {name}: {entry['remote']} ({'your own code; it deploys after Kirill approves' if entry.get('deploy') == 'self' else 'the change becomes a pull request'})"
        for name, entry in repositories.items())


def brief_prompt(intent: str, repositories: Dict[str, Dict[str, Any]], *, answers: List[str], memory: str = "") -> str:
    answered = f"\nKIRILL'S ANSWERS TO YOUR QUESTIONS:\n{_bullets(answers)}\n" if answers else ""
    context = f"\nWHAT YOU KNOW ABOUT THIS:\n{memory.strip()}\n" if memory.strip() else ""
    return f"""Kirill asked for a change to code. Write the brief Claude Code will work from: it makes the change in its own checkout, RMP runs the tests, and you review the result.

KIRILL'S REQUEST:
{intent}
{context}
REPOSITORIES:
{repo_lines(repositories)}
{answered}
You may read the repository to write a good brief, but do not change anything: Claude Code makes the change.
Reply with one JSON object and nothing else:
{{"repo": "<one of the names above>", "title": "<a few words for the branch name>", "goal": "<what to change and why>", "acceptance_criteria": ["<how to tell it is done>"], "constraints": ["<what must not change>"], "questions": []}}
Put a question in "questions" only when the change cannot be done well without Kirill's answer."""


def parse_brief(text: str, repositories: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The brief, with a question for Kirill when the repository is missing or unknown."""
    data = _json(text)
    if data is None:
        raise ValueError("the brief was not a JSON object")
    names = {name.lower(): name for name in repositories}
    names.update({entry["remote"].lower(): name for name, entry in repositories.items()})
    repo = names.get(str(data.get("repo") or "").strip().lower())
    goal = str(data.get("goal") or "").strip()
    questions = _strings(data.get("questions"))
    if not goal and not questions:
        raise ValueError("the brief has no goal")
    if repo is None and not questions:
        questions = [f"Which repository should I change: {', '.join(repositories)}?"]
    title = str(data.get("title") or "").strip() or " ".join(goal.split()[:6])
    return {"repo": repo, "title": title[:80], "goal": goal, "acceptance_criteria": _strings(data.get("acceptance_criteria")),
            "constraints": _strings(data.get("constraints")), "questions": questions}


def brief_text(brief: Dict[str, Any]) -> str:
    return (f"{brief['goal']}\n\nAcceptance criteria:\n{_bullets(brief['acceptance_criteria'])}"
            f"\n\nConstraints:\n{_bullets(brief['constraints'])}")


def system_prompt(entry: Dict[str, Any], branch: str, tests: List[List[str]]) -> str:
    commands = "; ".join(" ".join(argv) for argv in tests) or "(none declared)"
    return f"""You are working for Aura, Kirill's assistant, in a checkout of {entry['remote']} on branch {branch}.
- Work only inside this checkout. Do not push and do not change git remotes or configuration: RMP collects your commits.
- Commit your work with clear messages when it is done; anything left uncommitted is committed for you.
- Run the repository's tests before you finish: {commands}. RMP runs them again itself, so report what they really showed.
- This machine's services and secrets are out of reach on purpose; do not look for them.
- If the task is unclear or cannot be done, say so in the report instead of guessing.
- Finish with the structured report."""


def failing_tests(tests: Optional[Dict[str, Any]]) -> str:
    lines = []
    for command in (tests or {}).get("commands") or []:
        if not command.get("ok"):
            tail = "\n".join(str(command.get("tail") or "").splitlines()[-TEST_TAIL_LINES:])
            lines.append(f"$ {' '.join(command.get('command') or [])}\n({command.get('exit')})\n{tail}")
    return "\n\n".join(lines)


def rework_prompt(feedback: List[str], tests: Optional[Dict[str, Any]]) -> str:
    failing = failing_tests(tests)
    failing = f"\n\nRMP's test run failed:\n{failing}" if failing else ""
    return (f"Your change was reviewed and needs another pass.\n\n{_bullets(feedback)}{failing}\n\n"
            "Fix this in the same checkout, run the tests again, commit, and finish with the structured report.")


def change_request_prompt(words: str) -> str:
    return (f"Kirill reviewed your change and asked for this:\n{words}\n\n"
            "Make the change in the same checkout, run the tests again, commit, and finish with the structured report.")


def rebase_prompt(bundle: str, main: str) -> str:
    return (f"main moved after you started; it is now at {main[:12]}. Rebase your branch onto it: "
            f"`git fetch {bundle} main`, then `git rebase FETCH_HEAD`. Resolve any conflicts so the change still does "
            "what the brief asks, run the tests again, and finish with the structured report. Rebase; do not merge.")


def pr_body(summary: str, tests: Optional[Dict[str, Any]]) -> str:
    return (f"{summary}\n\n---\nTests (RMP's own run in an isolated checkout): {tests_line(tests)}\n"
            "Made by Aura with Claude Code; Kirill approved it in Slack.")


def tests_line(tests: Optional[Dict[str, Any]]) -> str:
    if not tests:
        return "not run"
    counts = "; ".join(", ".join(f"{v} {k}" for k, v in (c.get("counts") or {}).items()) or str(c.get("exit"))
                       for c in tests.get("commands") or [] if not c.get("setup"))
    return f"{'passed' if tests.get('ok') else 'FAILED'} ({counts or 'no output'})"


def review_prompt(brief: Dict[str, Any], number: int, claude: Dict[str, Any], evidence: Dict[str, Any], diff: str) -> str:
    collected, tests = evidence.get("collected") or {}, evidence.get("tests")
    report = claude.get("report") or {}
    ended = {"max_turns": "it ran out of turns before finishing"}.get(claude.get("kind"), "it finished")
    commits = "\n".join(f"- {c['sha'][:10]} {c['subject']}" for c in collected.get("commits") or []) or "- (no commits)"
    secrets = "; ".join(f"{s.get('kind')} in {s.get('path') or 'the diff'}" for s in collected.get("secrets") or [])
    shown = diff[:DIFF_CHARS_FOR_REVIEW]
    cut = " (cut short)" if len(diff) > len(shown) or collected.get("truncated") else ""
    return f"""Claude Code finished round {number} of the coding task ({ended}). Review its work against the brief, using RMP's evidence, and give your verdict.

BRIEF:
{brief_text(brief)}

CLAUDE CODE'S REPORT (its own claims):
{json.dumps(report, indent=1)[:4000] if report else "(no report)"}

RMP'S EVIDENCE (what actually happened):
Commits:
{commits}
Diffstat:
{(collected.get("diffstat") or "(no changes)").strip()[-2000:]}
RMP's own test run: {tests_line(tests)}
{failing_tests(tests)}
Dependency files changed: {", ".join(collected.get("dependencies_changed") or []) or "none"}
Secret scan: {secrets or "clean"}

DIFF{cut}:
{shown or "(empty)"}

Reply with one JSON object and nothing else:
{{"verdict": "ready" or "rework", "feedback": ["<what Claude Code must fix, when rework>"], "reply": "<your message to Kirill: what changed and why, RMP's test result, and anything he should know>"}}
"ready" only when the change does what the brief asks, RMP's tests pass, and nothing is left to fix."""


def parse_review(text: str) -> Dict[str, Any]:
    data = _json(text)
    if data is None:
        return {"verdict": "rework", "feedback": [], "reply": "", "error": "the review was not a JSON object"}
    verdict = str(data.get("verdict") or "").strip().lower()
    reply = str(data.get("reply") or "").strip()
    if verdict not in ("ready", "rework") or not reply:
        return {"verdict": "rework", "feedback": _strings(data.get("feedback")), "reply": reply,
                "error": "the review had no verdict or no reply"}
    return {"verdict": verdict, "feedback": _strings(data.get("feedback")), "reply": reply}


def final_prompt(review_reply: str, deployed: Dict[str, Any]) -> str:
    return f"""The coding task is done. Write your final message to Kirill.

YOUR REVIEW OF THE CHANGE:
{review_reply}

WHAT RMP DID AFTER HIS APPROVAL:
{json.dumps(deployed, indent=1)[:4000]}

Say what changed, how it was verified, and what happened after his approval, in plain language. Only state what the review and RMP's record above show."""
