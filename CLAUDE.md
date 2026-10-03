# CLAUDE.md

This is RMP, the orchestration layer behind Aura, Kirill's assistant. You work on it in one of two ways:

- **In Aura's direct session** (the usual way): Aura talks to you during one of her tasks. You run as root in a clone of this repository. A change reaches `main` only through a pull request whose CI test check passed; Aura merges and deploys it.
- **In a reviewed coding job**, when Kirill asks for one: you work in a job checkout as `aura-coder`. RMP collects your commits, runs the tests itself, Aura and an evaluator review the change, and nothing ships before Kirill approves it in Slack.

## Where things are

- `app/api/server.py`: the FastAPI service. Slack messages arrive at `POST /tasks`; it also serves signals and readiness.
- `app/task_registry/`: intake, which decides whether a message continues a running task, starts a new one or needs a question. Its prompt is in `intake_prompt.py`.
- `app/workflows/`: the Temporal workflows. `generic_task.py` answers Kirill, `catalog_task.py` runs the step templates in `catalog.py`, and `coding_task.py` runs coding tasks.
- `app/activities/`: Temporal activities (OpenClaw turns, database, coding).
- `app/orchestrator/`: the Process Evaluator, briefs and prompt policies.
- `app/coding/`: Claude Code. `direct.py` (Aura's direct sessions), `runner.py` (Claude Code units), `workspace.py`, `verify.py`, `github.py` (pull requests into the protected `main`), `deploy.py`, `records.py` (what Claude did, for memory and the evaluator).
- `app/reconciler.py`: repairs stuck and orphaned tasks.
- `plugins/`: OpenClaw plugins, copied to the gateway's plugin directory when a change deploys.
- `web-stack/`: Aura's web backends.
- `docs/CONCEPT_TREE.md` and `ARCHITECTURE.md`: the architecture. Read them before changing routing.

## Tests

- Python: the full suite with `-m pytest -q -p no:warnings`, using the interpreter named in your system prompt, or `/root/.openclaw/rmp/venv/bin/python` in a direct session.
- Plugins: `node --test tests/node/*.test.js`. In a direct session, the tests that compare the live plugin copies with the repository fail for a plugin you changed, until it deploys.
- A change to a workflow must keep `tests/test_workflow_replay.py` passing: recorded histories must still replay.
- Add or update tests with every change in behaviour. Never weaken or skip a test to make it pass.

## Invariants

- Every Slack DM goes plugin, `POST /tasks`, intake, Temporal, evaluator, and RMP posts the reply. OpenClaw never replies in Slack itself.
- Aura's replies reach Slack only after the Process Evaluator accepts them.
- Workflow code is deterministic: no I/O, clocks or randomness in workflow functions. Use activities, and `workflow.now()` for time.
- Don't add keyword rules that decide routing or catalog types; the intake LLM decides.
- No secrets in the repository, and never print tokens or keys.
- `.cursor/rules/rmp-architecture.mdc` is copied to the host when a change deploys; change it deliberately.

## Commits

- Small, focused commits. The message is one line in plain English saying what changed and why.
- Leave the history in `docs/*_PROGRESS.md` as it is; those logs are append-only.

## Pull requests (direct sessions)

- `main` is protected: nothing is pushed to it, and a pull request merges only when CI's `test` check passed.
- Work on a branch named `aura/<topic>`, run the tests, then `aura-github push` and `aura-github gh pr create` with a title and a body that says what changed and how you tested it. Tell Aura the pull request's URL.
- To wait for CI, run `aura-github gh pr checks <number> --watch`. If CI fails, fix the same branch.
- Merge only when Aura approves, with `aura-github gh pr merge <number> --squash`. RMP then deploys GitHub's `main` once CI passed on it and Aura is idle.
- Run focused tests for what you changed; CI runs the full suite on the pull request.
- Only a change to this repository needs a pull request. A one-off script or analysis Aura asks for stays in the workspace, uncommitted.
- Never edit the live checkout `/root/.openclaw/rmp` (or `/root/.openclaw/plugins`, `/root/.openclaw/web-stack`): a change there goes live at once, without CI.
- Use only `aura-github` for GitHub; never read or pass the token yourself. Other repositories are off limits unless Aura says Kirill asked for it.
