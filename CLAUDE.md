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
- Plugins: `node --test tests/node/*.test.js`.
- Workflow command-order changes go behind `workflow.patched("<new id>")` with a recorded fixture. CI refuses a diff under `app/workflows` without a new patch id, a new fixture key or the `no-replay-impact` label.
  - Record fixtures with `RECORD_HISTORIES=<repo>/tests/fixtures` by running the scenarios `tests/test_workflow_replay.py` names; re-recording replaces the history under a key.
  - The guard sees only `app/workflows/**`. The helpers the workflow files import also decide command order: `app/orchestrator/step_predicates.py`, `process_brief.py`, `decision_engine.py`, `completion_rework.py` and `app/notification_policy.py`. A change there needs the same patch and fixture; only `tests/test_workflow_replay.py` catches a miss there, and only on recorded paths.
  - The label is an auditable decision, not a security control: Aura's GitHub identity can add labels.
  - RMP's rollback revert of a workflow change (`aura/revert-<sha>`) waits for the `no-replay-impact` label and a manual merge with a merge commit (`aura-github gh pr merge <n> --merge`, the method RMP's revert uses). Until it merges, nothing deploys.
- The suite fails any test that touches production paths, this host's service ports or the host service tools (`systemctl`, `openclaw`, `claude` and the like). Point the module at `tmp_path` through its config variable instead.
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
- Leave the history in `docs/*_PROGRESS.md` as it is; those logs are append-only. CI refuses a rewrite unless the PR carries the `progress-log-rewrite` label. RMP's rollback revert (`aura/revert-<sha>`) of a change that appended to a log removes those entries, so it waits for that label and a manual merge with a merge commit (`aura-github gh pr merge <n> --merge`, not `--squash`). Until it merges, nothing deploys.

## Pull requests (direct sessions)

- `main` is protected: nothing is pushed to it, and a pull request merges only when CI's `test` check passed.
- Work on a branch named `aura/<topic>`, run the tests, then `aura-github push` and `aura-github gh pr create` with a title and a body that says what changed and how you tested it. Tell Aura the pull request's URL.
- To wait for CI, run `aura-github gh pr checks <number> --watch`. If CI fails, fix the same branch.
- Merge only when Aura approves, with `aura-github gh pr merge <number> --squash`. RMP then deploys GitHub's `main` once CI passed on it and Aura is idle.
- Run focused tests for what you changed; CI runs the full suite on the pull request.
- Only a change to this repository needs a pull request. A one-off script or analysis Aura asks for stays in the workspace, uncommitted.
- Never edit the live checkout `/root/.openclaw/rmp` (or `/root/.openclaw/plugins`, `/root/.openclaw/web-stack`): a change there goes live at once, without CI.
- Use only `aura-github` for GitHub; never read or pass the token yourself. Other repositories are off limits unless Aura says Kirill asked for it.
