# Direct Claude mode for Aura: progress log

Append-only. One entry per plan step: what changed, files, verification, deviations and why.

- **Plan:** `/root/.cursor/plans/direct_claude_mode.plan.md`.
- **Where the work happens:** the worktree `/root/.openclaw/rmp-direct` (branch `direct-claude`). `main` is fast-forwarded after each step.

---

## Step 1 — Probes

**Date:** 2026-10-02.

**Kirill's decisions:**
- Claude runs as root with full access, in Claude Code's `auto` permission mode, and direct mode is Aura's default way to use it. Safeguards against prompt injection come in a later phase.
- Aura talks to Claude turn by turn, in any task.
- Her own code changes go through GitHub: a branch and a PR on `openclaw-jev`, green CI, her own merge, then RMP deploys `main` with checks and automatic revert. Kirill gets a Slack note with the PR link.
- Other repositories stay off limits unless Kirill says otherwise for a particular change.
- The structured coding workflow stays, for when he asks for a reviewed job.
- Every Claude session is recorded in the platform memory, like her other tools.

**Baseline:**
- `main` is at `af8a5ee`, CI green (run 36977534716).
- OpenClaw 2026.9.7 (`c074824`); Claude Code 2.1.280 (the pinned install in `aura-coder`'s home).
- Readiness: 44 pass, 1 warn (telemetry, by design), 0 fail.

**Probes.** All ran live, in a scratch folder under `/tmp` with temporary config directories, using the existing login token from `/etc/aura-coder/claude.env`.

| Probe | Result |
|---|---|
| Root, `claude -p --permission-mode auto --session-id <uuid>` | Ran; read the file; kept the assigned session ID; no permission denials |
| Second turn with `--resume <uuid>` | Remembered the first turn, made an edit in the folder, same session ID |
| Session record | Claude writes the full transcript to `$CLAUDE_CONFIG_DIR/projects/<folder>/<session>.jsonl` |
| Host policy and root | `gh --version` was denied by the server-wide deny rule: the policy binds root too |
| `--settings` deny rules | Ignored under today's `allowManagedPermissionRulesOnly`: `echo` ran despite a `Bash(echo *)` deny |
| Policy file bound into one unit | `systemd-run -p BindReadOnlyPaths=<copy>:/etc/claude-code/managed-settings.json` showed the copy inside the unit; the host file was unchanged |

**OpenClaw facts:**
- **Session key.** Plugin tools can be registered as factories, `api.registerTool((ctx) => tool, { name })`, and `ctx` carries `sessionKey` and `agentId` (as in the bundled `memory-wiki` extension). Aura's task sessions are `agent:main:rmp_task_<id>`, so the Claude tool can tie each session to its task.
- **OpenClaw's time limit.** A gateway turn has a 48-hour budget when `agents.defaults.timeoutSeconds` is unset (OpenClaw docs: `cli/agent.md`, `gateway/cli-backends.md`); it is unset here, and plugin tools have no timeout of their own.
- **RMP's time limits are the real constraint.** `_dispatch_openclaw_session` waits at most 600 s for Aura's reply, and the plan-step activity in `generic_execute_child.py` has a 45-minute limit with a 12-minute heartbeat.
- **No false stalls.** The stall check (`_jsonl_agent_stalled`) skips a turn whose last assistant message stopped for a tool call, so a long Claude turn is not mistaken for a stall.

**Research (sources):**
- [Claude Code sandboxing](https://code.claude.com/docs/en/sandboxing): `--dangerously-skip-permissions` refuses to run as root outside a recognized sandbox, and auto mode has a classifier review each action.
- [Claude Code settings](https://code.claude.com/docs/en/settings):
  - managed settings override everything, including `--settings`, which can't set managed-only keys;
  - `CLAUDE_CONFIG_DIR` relocates user settings, history and plugins.
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli): `--session-id`, `--resume`, `--permission-mode` (`default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`), `--setting-sources`, `--strict-mcp-config`, `--mcp-config`, `--max-turns`.
- Claude Code issues [9184](https://github.com/anthropics/claude-code/issues/9184) and [3490](https://github.com/anthropics/claude-code/issues/3490): `IS_SANDBOX=1` skips the root check for bypass mode. It isn't needed here, because auto mode runs as root.

**Design consequences and deviations from the plan text:**
- **Step 2.** The plan said coding jobs would stay strict through per-run flags. Those flags are ignored under today's policy and cannot set managed-only keys. Instead, coding-job units bind today's strict policy file over the host path, and only the host file shrinks for direct mode. Coding jobs keep exactly today's rules.
- **Step 3.** Turns use `--session-id`, then `--resume`. The transcript Claude writes in the session's config directory is the full record that step 4 puts into memory. Aura's turn deadline extends while her Claude turn works.

**Clean-up:** the probe folders and outputs under `/tmp` were removed.

---

## Step 2 — Two Claude profiles

**Date:** 2026-10-02.

**What changed:**
- **Coding units.** `unit_properties` binds `/srv/aura-code/policy` over `/etc/claude-code` (`BindReadOnlyPaths`). Claude Code reads its policy file, the drop-ins in `managed-settings.d/` and managed MCP servers (`managed-mcp.json`) from that directory ([managed settings docs](https://code.claude.com/docs/en/managed-settings)). So every coding unit sees exactly today's lockdown and nothing from the host. `MANAGED_SETTINGS` now names the coding copy, so the readiness and smoke comparisons keep their meaning.
- **Host policy (the direct profile's).** `ops/claude_host/managed-settings.json`, installed at `/etc/claude-code/managed-settings.json`, pins updates, turns off nonessential traffic and keeps the 30-day cleanup. It restricts nothing.
- **Setup.** `ops/setup_aura_coder.sh` installs both files.
- **Smoke.** A new in-unit probe, "coding policy in place": `/etc/claude-code` holds only `managed-settings.json`, with the coding policy's hash.

**Files:** `app/coding/units.py`, `ops/claude_host/managed-settings.json`, `ops/setup_aura_coder.sh`, `ops/claude_code_smoke.py`, `tests/test_coding_host.py`.

**Rollout order:**
1. The coding copy was installed, identical to the old host file.
2. The code was deployed; the API and worker reloaded 8 s after the fast-forward.
3. The host policy was relaxed.
4. The smoke check ran.

No coding job was running.

**Verification:**
- **Tests:** `test_coding_host.py` and `test_coding_observability.py`, 30 passed.
- **Smoke (live):** isolation ok, including "coding policy in place" while the host file already differed; Claude in a coding unit answered (`claude-opus-5-5`, `bypassPermissions`).
- **Behavior (live):** the same request to run `gh --version` was denied inside a coding unit and ran as root in auto mode on the host.
- **Readiness:** 44 pass, 1 warn (telemetry), 0 fail; `claude_code`, `coding_isolation` and `coding_jobs` pass.

**Deviation:** the plan's per-run flags were replaced by the bind (see step 1).

---

## Step 3 — RMP direct sessions

**Date:** 2026-10-02.

**What changed:**
- **Sessions (`app/coding/direct.py`).** A session belongs to a task. Its `session.json` and workspace live under `/srv/aura-code/direct/<task>/<session>/`, root-only, so coding units can't see them. The workspace is either:
  - `repo`: a clone of `openclaw-jev` from GitHub, borrowing the live checkout's objects (`--reference-if-able ... --dissociate`) and then standing alone, with the commit identity "Aura (Claude Code)";
  - `scratch`: an empty folder.

  Never the live checkout.
- **Turns.** Each turn is `claude -p` as root, in auto mode, in its own transient unit `aura-direct-<task>-<session>-<n>`:
  - `--session-id` the first time, `--resume` once Claude Code has the conversation;
  - `CLAUDE_CONFIG_DIR=/root/.claude`, the login token from `/etc/aura-coder/claude.env`;
  - resource limits as for coding jobs, and a 60-minute turn limit (`direct_turn_timeout_sec`);
  - a short system prompt: work in the workspace, never edit the live checkout, never push to `main`, other repositories only on Kirill's word, no secrets in output, ask when unclear.

  `Turn` subclasses the runner's `Run`, so stream reading, stop and usage booking are the same code as for coding runs.
- **Limits.** One turn at a time per session, two at once on the host (`direct_max_running`), messages up to 120 KB.
- **API** (behind the RMP key):
  - `POST /api/claude/sessions`, only for a task that is still running, found from Aura's session key;
  - `POST .../messages`;
  - `GET .../turns/{n}?wait=` (a long poll of up to 55 s);
  - `POST .../end`;
  - `GET /api/claude/sessions[/{id}]`.

  The task's timeline gets `claude.session_started`, `claude.turn_started` and `claude.session_ended` events.
- **Stop.**
  - `abort_task_runs`, which Kirill's stop, a cancel and a supersede all go through, ends the task's Claude sessions first, since their turns are Aura's tool calls.
  - `stop_task_units` does the same for the coding and reconciler paths.
  - The reconciler ends sessions whose task has finished, and prunes ended sessions' workspaces after the job retention (14 days); their records stay.
- **Aura's turn time.**
  - In `_dispatch_openclaw_session`, the 10-minute reply deadline moves on while one of the task's Claude turns runs (`_reply_deadline`); a hard deadline, as in intake's quick turns, never moves.
  - The seven 45-minute activity limits on Aura's turns are now one `AURA_TURN` of 4 hours (`app/workflows/timeouts.py`). Turns without Claude are still bounded by the 10-minute reply deadline.

**Files:**
- **New:** `app/coding/direct.py`, `app/workflows/timeouts.py`, `tests/test_direct_sessions.py`.
- **Changed:** `app/api/server.py`, `app/openclaw_control.py`, `app/activities/coding_activities.py`, `app/activities/openclaw_activities.py`, `app/reconciler.py`, `app/config.py`, and the seven workflows.

**Verification:**
- **Tests:**
  - `test_direct_sessions.py`, 15 passed: the root profile and auto mode in the unit, resume on the second turn, one turn at a time, the host limit, end and stop, a stop or an abort ends only the task's sessions, usage booked once, a lost unit, refused input, a real-git clone, prune, the reply deadline, the reconciler, and the API end to end with events.
  - Full suite: 1025 passed, 4 skipped.
- **Live, as root with real Claude:**
  - the clone took 3.9 s from GitHub, standing alone afterwards;
  - turn 1 succeeded in 17 s; turn 2 resumed and remembered the first;
  - ending the session mid-command (`sleep 120`) took 0.4 s and the turn reads as stopped; no unit was left.
- **Readiness:** 44 pass, 1 warn (telemetry), 0 fail; the reconciler sweep runs without errors.

**Found and fixed live:** the clone flag is `--dissociate`; I had written `--dissolve`, which git rejects. The faked-git test could not see it, so `_clone` now takes its URL and a real-git test covers it.

**Noted:** the clone is GitHub's `main`, which is behind the local `main` until this work is pushed.

---

## Step 4 — Claude's work in memory

**Date:** 2026-10-02.

**Why:** Kirill's addition to the plan. Aura's other tools leave their trace through her OpenClaw transcript, which RMP already reads: the evaluator sees her actions, and the task document lists every tool call and keeps the pages and files she read. Claude is the one path outside that transcript: RMP runs it, so what it did is only in RMP's records. The structured coding jobs had the same gap.

**What changed:**
- **One reader (`app/coding/records.py`).** `task_records(task_id)` reads RMP's own records into conversations of turns: each direct session, and the coding job's runs (the brief's goal as the first ask, plus RMP's test result per round). Each turn has what Aura asked, Claude's reply (its report's summary for coding runs), commands, files edited, pull-request links and tokens. `section_text` and `conversation_text` render them. The evaluator's evidence will use the same reader in step 7.
- **The task document (`app/deep_memory/ingest.py`).**
  - A fifth section, "Claude sessions", holds a digest of every conversation.
  - Each conversation is also its own `claude_session` document, with a heading per turn and a `part_of` link to the task.
  - Both are redacted, chunked, enriched and indexed like the rest. Enrichment gets a label for the new kind.
- **Redaction (`app/memory/policy.py`).** `sk-` keys may now contain `-` and `_`, which covers OpenAI's `sk-proj-` keys and Claude Code's `sk-ant-oat01-` token, and fine-grained GitHub tokens (`github_pat_`) are covered. Before this, neither the Claude token nor the GitHub token on this host would have been redacted, and root Claude can read both.
- **Invariant `claude_records` (`app/deep_memory/health.py`).** It fails when a finished user task with Claude work lacks the "Claude sessions" section in its document 30 minutes after it ended.

**Files:**
- **New:** `app/coding/records.py`, `tests/test_claude_records.py`.
- **Changed:** `app/deep_memory/ingest.py`, `app/deep_memory/enrich.py`, `app/deep_memory/health.py`, `app/memory/policy.py`, `tests/test_deep_ingest.py`, `tests/test_deep_memory_health.py`, `tests/test_memory_policy.py`.

**Verification:**
- **Tests:**
  - records from a real direct session (through the fakes) and from a coding job with a brief, a pull request and RMP's tests;
  - the task document's new section and the linked conversation document, with a Claude token redacted;
  - the invariant on missing, fresh, internal and plain tasks;
  - the token shapes on this host.
  - Full suite: 1031 passed, 4 skipped.
- **Backfill (live).** The one finished task with Claude records, the coding task `bd56025f` from the earlier acceptance, was re-queued. Its document now has sections Conversation, Path history, Actions and Claude sessions, enriched. Its conversation is an enriched `claude_session` document (5 chunks, a section for the turn).
- **Invariants:** `claude_records` passes (1 of 1), `task_documents` passes (7 of 7).

---

## Step 5 — Aura's Claude tools

**Date:** 2026-10-02.

**What changed:**
- **The tools (`plugins/rmp_adapter/claude_tools.js`).** Four tools for Aura, registered by `rmp_adapter`:
  - `claude_start` starts a session in `repo` (a GitHub clone of her repository) or `scratch`, tied to the task through the tool's `context.sessionKey`;
  - `claude_send` sends a message and waits for Claude's answer: its text, the files it edited, the commands it ran, what auto mode blocked, and a usage limit with its reset time;
  - `claude_status` reports where a session stands, optionally waiting up to 55 s;
  - `claude_end` ends a session.

  All four only call RMP's session API, through the plugin's existing `rmpFetch`, which carries the RMP key and redacts it in logs.
- **Waiting.** `claude_send` long-polls RMP in steps of at most 50 s until the turn is done. It rides out up to 5 minutes of RMP being unreachable, as during a code reload, because the turn runs on in its own unit. It gives up at once on a refused request (HTTP 4xx), and stops when Aura's run is aborted.
- **Manifest.** `openclaw.plugin.json` declares the four tools under `contracts.tools`. `index.js` exports `rmpFetch` for the tests.

**Deviation:** the plan named a new plugin `plugins/aura_claude/`. The tools live in `rmp_adapter` instead: it is already registered and allowed in `openclaw.json`, and it already holds the RMP client and key handling. So nothing in OpenClaw's configuration changed and there is no second RMP client.

**Found live:** OpenClaw 2026.9.7 drops plugin tools that the manifest does not declare. The first gateway restart logged "plugin must declare contracts.tools" for all four. A node test now ties the registered tools to the manifest, and compares the live copies with the repo.

**Deploy:**
- **First (12:52 local):** the files were copied to `/root/.openclaw/plugins/rmp_adapter/` with no user task active, and the gateway restarted.
- **Second (12:55):** the manifest, again with no task active. After it the gateway came up in 45 s with no warnings.

**Incident, investigated at Kirill's request:**
- **What was seen.** The commit `12e42de`, the fast-forward of `main`, the manifest copy and the second restart (12:55:55 to 12:55:57) did not come from a command whose result I saw. My visible command found nothing to commit.
- **What I found.** The journal and both reflogs show the same chain as my command, two seconds from commit to restart, gated on no active task. Today's only gateway stops are this one and my first. No other process, terminal or agent transcript was active. Every other commit and fast-forward today is mine.
- **Conclusion:** an earlier attempt of my own command ran in full and its result never reached me (inferred). Its outcome was the intended state.
- **From now on,** deploy commands check the live state first, so a repeated run changes nothing.

**Verification:**
- **Node:** 26/26, including the claude tools end to end against a fake RMP (start, a send across three polls, end), an RMP outage ridden out, a refused message, a usage limit, progress while working, the manifest check, and the live copies.
- **Gateway:** active; `http server listening (6 plugins ...)` with no `contracts.tools` warnings.
- **Not yet:** Aura using the tools in a real task, which comes with step 7's notes and the acceptance.

---

## Step 6 — Aura's code through GitHub pull requests

**Date:** 2026-10-02.

**Kirill's decisions:**
- `main` is protected for everyone, RMP included: every change lands through a pull request with green CI.
- Aura needs no approval for her own changes: once CI is green she merges, RMP deploys, and Kirill gets a note with the link. He can always roll back to an earlier `main` from GitHub.
- The approval card stays only for reviewed jobs he explicitly asks for, and those ship through a pull request too.

**What changed:**
- **`app/coding/github.py`.** It calls the GitHub API through `gh`, with the token only in that command's environment: a pull request, the `test` check on a commit (latest run), waiting for it, merging at an exact commit, opening a pull request or reusing an open one, `land` (push a commit as a branch, open its pull request, merge once the check passed) and `protect_main`.
- **`deploy_pr` (`deploy.merge_pull_request`, `POST /api/claude/deploy`, the tool in `rmp_adapter`).** RMP squash-merges Aura's pull request only if all of these hold:
  - it is open, into `main`, from a branch of her repository;
  - its `test` check passed;
  - GitHub's `main` contains the live `main`.

  Then it starts the task's deploy unit. A deploy that is already waiting just picks up the later merge. Events: `coding.pr_merged` or `coding.pr_not_merged`.
- **The deploy unit.**
  - A deploy of GitHub's `main` waits until no user task is active, so it never restarts Aura's own run. Then it fetches GitHub's `main`, and fast-forwards, syncs, restarts and checks as before.
  - A reviewed job's approved commit first lands through its own pull request; CI failing there deploys nothing.
  - No deploy pushes `main` any more.
  - A failed deploy still reverts the live code at once. The revert then lands as a pull request merged with a merge commit, so the live `main` stays an ancestor of GitHub's.
  - After Aura's own deploy, Kirill gets a note (`notify_ops_slack`) with each pull request's link. A reviewed job still gets its judged reply run.
- **`aura-github` (`ops/aura_github.sh`, installed in `/usr/local/bin`).** Claude's way to GitHub in direct sessions: `push` a branch and `gh` on her repository, with the token read inside the command. It refuses pushing `main`, a clone of another repository, and `gh pr merge`. The direct sessions' system prompt says how to open a pull request and that Aura merges it.
- **A race fixed.** A turn's status read its stream before its exit line, so a turn finishing between the two reads looked cut short (`no_result`). A test that failed two runs in three under load exposed it. The exit line is now read first, here and in the records reader, and a test pins that order.

**Deploy:**
- **Code:** commit `11bfce2`; the API and worker reloaded.
- **Wrapper:** installed.
- **Plugin:** `claude_tools.js` and the manifest with `deploy_pr`; the gateway restarted with no task active.
- **Push:** `main` went to GitHub before protection (`af8a5ee..11bfce2`, the last direct push); CI [run 37003130210](https://github.com/Hyper-AI-Lab/openclaw-jev/actions/runs/37003130210) passed.
- **Protection:** on `main`, read back from the API: pull request required (0 approvals), `test` required, enforced for admins, no force pushes or deletions.

**Verification:**
- **Tests:** full suite 1063 passed, 4 skipped; node 27/27.
- **New tests:**
  - the token only in `gh`'s environment;
  - the check's states;
  - reusing an open pull request;
  - landing through real git;
  - the protection body;
  - every refusal of `deploy_pr`;
  - a deploy of GitHub's `main`, nothing new, a live `main` ahead of GitHub, a reviewed change failing CI, a rollback whose revert cannot reach GitHub;
  - the unit telling Kirill;
  - `aura-github` pushing a branch, refusing `main`, another repository and a merge;
  - the deploy API.
- **This entry** landed through a pull request with `github.land`, the reviewed-job path, under the new protection.

---

## Step 7 — What Aura and the other agents know

**Date:** 2026-10-02.

**What changed:**
- **Aura's notes** (`/root/.openclaw/workspace/TOOLS.md` and `AGENTS.md`). The coding section is now "Working with Claude Code":
  - Claude is her tool in any task, used when it helps;
  - how to brief it and talk to it;
  - her code changes go through a branch and a pull request, and `deploy_pr` once CI passed;
  - in her reply she says "merged, deploys when I'm done", never "deployed";
  - never live code, never `main`, other repositories only on Kirill's word, secrets.

  The reviewed coding job follows as the option Kirill asks for, with her brief, review and final reply. Her workspace repository was not committed: it already held unrelated uncommitted changes.
- **Intake** (`intake_prompt.py`). `coding_task` is only an explicitly requested reviewed job. Any other change to Aura's own code is `structured_work` with no catalog hint. The advisory keyword patterns stay: their hits are advisory, the rule defines the type, and the tests pin them.
- **The evaluator.** Its external evidence gains `_direct_claude_evidence`: Aura's direct sessions (the records digest, redacted), the pull requests RMP merged or refused for her, and their deploys. A reviewed job's own evidence stays as it was. The rule "tests pass only when RMP's own test run passed" now adds "or CI's test check passed on a pull request RMP merged".
- **`CLAUDE.md`.** The two ways Claude works here, the new modules, the test command for direct sessions, and how a direct session ships through a pull request.

**Verification:**
- **Tests:** the new tests and the related suites, 131 passed.
- **Landed** as [PR #2](https://github.com/Hyper-AI-Lab/openclaw-jev/pull/2) through `github.land`, after CI's full suite passed (squash `84b8b15`).

---

## Step 8 — Monitoring, docs and the rule

**Date:** 2026-10-02.

**Found:** two invariants would have failed on every deploy of Aura's own pull requests:
- `approved_deploys` wanted Kirill's approval, which by his decision they don't need;
- `deploy_verification` wanted RMP's exact-commit suite, where CI is their suite.

Deploy records now carry `source` (`github` or `reviewed`), and each invariant applies its rule to its own kind. A deploy of a `main` that was already live is now `unchanged`, not `deployed`.

**What changed:**
- **Readiness `claude_direct`.** It checks:
  - the host policy and `/usr/local/bin/aura-github` match the repository's copies;
  - GitHub's `main` is protected for everyone with the `test` check;
  - GitHub's `main` contains the live `main`.

  GitHub being unreachable is a warning. The details count open sessions and running turns. `live_units` recognises `aura-direct-*` units.
- **Invariants:**
  - `direct_units`: no direct turn without a live task;
  - `merged_deploys`: every merge recorded a passed CI check and was deployed within 3 hours (the deploy waits up to 2 hours for idle);
  - `approved_deploys`: reviewed jobs only;
  - `deploy_verification`: for Aura's deploys, CI's check recorded at the merge, before the deploy, plus the canary.
- **Docs:**
  - `README.md` (Coding);
  - `ARCHITECTURE.md` §5.11, rewritten as Claude Code: direct sessions, shipping through the protected `main`, reviewed jobs, observability, plus the `/api/claude/*` endpoints;
  - `docs/CONCEPT_TREE.md` (the workflow list and the coding invariants).
- **Rule item 6** in `.cursor/rules/rmp-architecture.mdc`, rewritten for both modes; the host copy is updated once this lands.

**Note:** for the second time today, an edit I could not see applied before my visible attempt (two ARCHITECTURE bullets, worded slightly differently). Their content is what I intended. I checked the section for duplicates, and the diff, before committing.

---

## Step 9 (in progress) — Acceptance

**Date:** 2026-10-02.

**Test 1, Aura talks to Claude (task `42e49f62`): passed.**
- Intake made it a normal task (`create_fresh`, no catalog type).
- Aura opened a `repo` session and sent one turn; Claude answered in about 40 s.
- Her reply matched Claude's findings and the evaluator accepted it.
- The reconciler ended the session 31 s after the task finished.
- The task document has its "Claude sessions" section, and the conversation is its own document.

**Test 2, a change through a pull request (task `68806177`): failed, then shipped another way.**
- Claude's turns 1 and 2 were stopped after about 5.5 minutes each, while their session stayed open.
  - Claude was running RMP's full test suite in its clone. `test_reconciler_janitor` runs the real `reconcile_once()`, which includes the step 3 sweep that ends sessions of finished tasks.
  - The sweep read the real session directory, found Claude's own open session, did not find its task in the test's database, treated it as finished, and stopped the running turn.
  - The SIGINT reached the whole unit, the test process included, so the test died before marking the session ended.
  - Evidence: systemd logged SIGINT "on client request" to `claude`, `bash`, `python`, `tail` and a `systemctl` inside the turn's own unit.
- At 13:20:47 RMP gave up waiting for Aura's turn ("Timed out waiting for agent reply"). The step 3 extension of her reply deadline only applied while a Claude turn ran, and she was between turns.
  - RMP retried while her original run still held the session.
  - The gateway answered 503 for three minutes; its docs list single-run admission timing out among the causes.
  - The task was compensated at 13:23:38, and its session ended with it.
- Her context reached 82,307 tokens, past the 60,000 limit of the `llm_usage` canary. She had run about 70 commands of her own through the code harness (308,000 characters of output), starting in the live checkout. She changed nothing there: the checkout was clean.
- Before the compensation, she had scheduled a cron follow-up. At 13:31 it called `deploy_pr` for PR #6, RMP merged it (CI passed), and the deploy unit shipped it at 13:34: `rmp-api` and `rmp-worker` restarted, and health, readiness and the canary passed. Kirill's note was sent.
- `deploy_pr` returned "internal_error" to Aura anyway. OpenClaw's code harness reads a tool's result as an object (`'details' in result`), and the plugin's tools returned bare strings.

**Kirill's decision:** Claude does the coding, merging included; Aura manages it. RMP records, and puts GitHub's `main` live once Aura is idle.

**What changed:**
- **Merging.** Claude merges with `aura-github gh pr merge --squash` once Aura approves; the wrapper no longer refuses merges, and the protected `main` still demands the `test` check. `deploy_pr`, its API endpoint and `merge_pull_request` are gone.
- **Going live.**
  - The watcher, `watch_main`, runs in the reconciler loop every minute and is skipped in development mode.
  - Once GitHub's `main` has moved past the live `main` and CI's `test` check passed on that exact commit, it starts `aura-deploy-main` for that commit.
  - The unit waits until no user task is active, then deploys as before, records a `coding.deploy` event on entity `deploy/main` with the CI result, writes `result.json`, and sends Kirill the note.
  - A commit whose deploy was blocked, failed or rolled back is not retried; a later commit is.
- **The sweep** ends only sessions whose task the database knows to be finished.
- **The test suite** gets its own empty session directory in every test (autouse fixture in `tests/conftest.py`).
- **The reply deadline** stays open while Aura works with Claude: a turn running, or an open session whose last turn ended (or which started) less than 10 minutes ago (`task_working`).
- **The tools** return OpenClaw's result object (`content` plus a small `details`). Claude's answer is cut at 12,000 characters instead of 30,000; the whole answer stays in the record.
- **The evaluator** sees Aura's sessions and, for each pull request linked in them, GitHub's word: merged or not, and CI's check on it. The rule: tests pass when RMP's own run passed, "or GitHub reports CI's test check passed on the pull request".
- **Invariants:**
  - `main_deployed` replaces `merged_deploys`: GitHub's `main` goes live within 3 hours;
  - `deploy_verification` takes the CI result recorded on a deploy of GitHub's `main`.
- **Notes and docs:**
  - Aura's notes: leave the work to Claude; she reviews, approves, and has Claude merge.
  - `CLAUDE.md` and the direct sessions' system prompt: run focused tests, since CI runs the full suite; merge only on Aura's approval.
  - `ARCHITECTURE`, `README`, `CONCEPT_TREE` and rule item 6, both copies.

**Verification:** full suite 1063 passed, 4 skipped; node 25 of 27 before the plugin deploy (the 2 live-copy checks).

---

## Step 9 — Acceptance, on the new flow

**Date:** 2026-10-02.

**Test 2 again, a change through a pull request (task `ae08907d`): passed.**
- Claude's first turn ran for about 18 minutes. It made the change on `aura/direct-turns-running` and ran the full suite in its clone. That suite had killed its turns before; this time the turn survived. It then opened PR #8 and watched CI pass.
- Aura reviewed the work and approved, and Claude merged PR #8 in its second turn (`test: SUCCESS`, 15:01:44Z). It then checked that the merged tree matched its branch.
- The evaluator accepted her reply (15:05:32Z), and the reconciler ended the session 41 s later.
- The watcher started `aura-deploy-main` once CI passed on `daf5864`. The unit restarted `rmp-api` and `rmp-worker`, the checks passed, and Kirill got the note at 15:07:41Z.
- Aura made 110 tool calls (174,000 characters of results), 28 of them her own shell commands. Most of the rest were `claude_status` checks, each carrying up to ten commands. Her context stayed under the canary's limit.

**A plugin-only deploy was rolled back for nothing (PRs #9, #10, #11).**
- PR #9 made `claude_status` answer with Claude's latest three steps only while it works, and wait up to 55 s by default.
- The watcher deployed it and restarted only the gateway, as the restart map says for `plugins/`.
- `runtime_code_sync` then failed. It counted `plugins/rmp_adapter/*.js` as code the API and worker run, so they looked stale. That inclusion dates from 2026-08-11 (`cb0fcd6`). The code watcher reloads them for Python only, and the gateway is what runs the plugin.
- The unit reverted the live code, landed the revert as PR #10, and told Kirill. The watcher then deployed the merged revert, which changed nothing, restarted nothing and passed.
- The fix, PR #11:
  - `watched_code_paths` counts only the Python the API and worker import. A new test checks that a plugin edit leaves them in sync; it fails without the fix.
  - It also re-applies PR #9's change.
- The watcher deployed PR #11 on its own: `openclaw-gateway`, `rmp-api` and `rmp-worker` restarted, CI success, canary ok. Node then passed 27 of 27 against the live plugin.

**Test 3, stop during a Claude turn (task `3ea38568`): passed.**
- Claude was running the full suite when Kirill replied "stop" (15:59:14Z).
- His notice was delivered and Aura's run aborted at 15:59:15Z. At 15:59:16Z the task was `stopped_by_user`, the session had ended with reason `stop`, and the turn's unit was gone.
- The turn is recorded as `stopped`. The task document was enriched with its "Claude sessions" section.

**State:** readiness 48 pass, 1 warn (telemetry); invariants 15 of 15 pass.

**Left as they are:**
- A deploy that changes no files, such as a merged revert, still sends Kirill an "Aura's change is live" note.
- The ledger row of an RMP notice still fails its foreign key after the Slack message is sent. That predates this work.

---

## Models: `opusplan`, planning turns, effort; Claude Code 2.1.288

**Date:** 2026-10-03.

**Before:**
- Every Claude turn, in direct sessions and reviewed jobs, ran on Opus 5.5 (`--model opus`, `--fallback-model sonnet`) at its default `medium` effort.
- Each result's `modelUsage` listed only `claude-opus-5-5`.

**Kirill's decision:**
- Aura's direct sessions use `opusplan`, with planning turns.
- Claude Code goes up to 2.1.288, so the work runs on Sonnet 5.5 rather than Sonnet 5.
- Aura chooses the effort per turn.
- Reviewed jobs stay on `opus`.

**Probe on 2.1.280, headless, the way RMP runs a turn:**
- A planning turn (`--permission-mode plan --model opusplan`) ran on `claude-opus-5-5`. Claude read the code, wrote the plan to `/root/.claude/plans/`, answered with it and changed nothing. Nothing waited for a plan approval.
- The next turn (`--resume`, auto mode) ran on `claude-sonnet-5` and implemented the plan.
- Both turns' `init` events name `claude-sonnet-5`, so the stream's own model is not the model a planning turn ran on. A result's `modelUsage` also counts the whole session.

**What changed:**
- **Config:** `direct_model: "opusplan"` for direct sessions; `model: "opus"` stays for reviewed jobs; the pin is `claude_version: "2.1.288"`.
- **Sending a turn:** `direct.send(..., plan=False, effort=None)` runs a planning turn with `--permission-mode plan`, and passes `--effort` for `medium`, `high` or `xhigh`; anything else is refused. `meta.json` records `plan` and `effort`.
- **What the records show:** `stream.StreamState.models` lists the models Claude's messages came from (Claude Code's own `<synthetic>` messages excluded). `status()` and the records carry `plan`, `effort` and `models`. Memory shows a turn as "(success; planning turn; claude-opus-5-5; effort high)".
- **API and tool:** `POST /api/claude/sessions/{id}/messages` takes `plan` and `effort` and records them on `claude.turn_started`. `claude_send` takes `plan` and `effort`, and a planning turn's answer starts "Claude, turn N (planning)".
- **Aura's notes:** a new bullet, "Planning, models and effort". For anything non-trivial she plans first, reviews the plan, then gives the go-ahead. A quick question needs no planning turn, and stuck work goes back to planning. Effort is `medium` for clear-scope work, `high` for bug fixes, `xhigh` for hard investigations.
- **Docs:** ARCHITECTURE, README, CONCEPT_TREE and rule item 6 (both copies). The stale "while one of her turns runs" and "pull requests RMP merged" lines in ARCHITECTURE are fixed.

**Upgrade order:**
- A version mismatch fails `claude_code` readiness, and the deploy's checks would roll the deploy back.
- So Claude Code 2.1.288 goes in with `CLAUDE_CODE_VERSION=2.1.288 bash ops/setup_aura_coder.sh` after the merge, while CI runs on `main` and before the watcher's deploy checks it.
- The smoke test then runs on the new version.

**Verification:** full suite 1067 passed, 4 skipped; node 27 of 28 before the plugin deploy (the live-copy check).

---

## Pull requests only for her code; files to Kirill with her reply

**Date:** 2026-10-03.

**Kirill's question:** does Aura always end with a pull request, even when a task (a file conversion, say) needs none?

**Findings:**
- Nothing in RMP forces a pull request. Claude's system prompt and `CLAUDE.md` require one only for "a change to Aura's code".
- Three things nudged her toward one:
  - Her planning bullet said the go-ahead means Sonnet "implements, tests and opens the pull request".
  - `claude_start` defaults to a clone of her repository without saying when to use `scratch`.
  - Every test task so far was a change to her code.
- Aura could receive Kirill's files (attachments arrive as local paths) but not send one back: RMP's Slack path was `chat.postMessage` only.
- The bot already holds `files:write` and `im:write`.

**Her notes (live at once, outside the repo):**
- "A pull request is only for your own code": scripts, conversions, data work and analyses go in a `scratch` session, with no branch, pull request or merge.
- "Review before you approve a merge": Claude walks her through what changed and why, the tests, the risks and CI, and she approves only when satisfied.
- "Leave the work to Claude" now covers diffs: no `git diff`, `gh pr diff` or reading the branch's files herself.
- The planning bullet no longer implies a pull request, and "Claude Code (Opus)" is now "Claude Code".

**Code:**
- `claude_start` says `repo` is for her own code and `scratch` for everything else.
- Claude's system prompt says only a change to Aura's code needs a branch and a pull request, and that other work stays in the workspace, reported with the full path of every file Aura should send. `CLAUDE.md` says the same for repository sessions.
- **Files to Kirill:**
  - `attach_file` (plugin) and `POST /api/replies/files` take a file from one of the task's Claude sessions while the task runs.
  - `app/coding/outbox.py` checks it: the resolved path lies under `/srv/aura-code/direct/<task>/`, it is a regular file of 1 byte to 50 MB, and its text doesn't match `redact_secrets`.
  - At most 10 files wait. RMP records a `reply.file_attached` event with the file's SHA-256.
- **Sending:**
  - `notify_slack_user` answers `delivered_with_files` for a delivered reply with files waiting.
  - `_deliver_final` then runs `deliver_reply_files`, behind `workflow.patched("reply-files")`, with 10 minutes per attempt and at most 5 attempts. Its failure is caught: a file never fails or holds up the task.
  - `side_effects.send_reply_files` checks each file again (same SHA-256), then uploads it with Slack's external upload into the DM (`conversations.open`) and records a `SideEffectReceipt` and a `reply.file_sent` event, once.
  - A file that changed or that Slack refuses gets a receipt, a `reply.file_refused` event and a notice to Kirill. A Slack outage raises for a retry, and the last attempt tells Kirill which files did not go.
- **Evaluator:** the evidence lists the files waiting, and a new rule says a file Aura says she sends counts only if the evidence lists it.

**Verification:** full suite 1077 passed, 4 skipped; node 27 of 29 before the plugin deploy (the two live-copy checks).

**The first live file (task `c8ab0794`, a CSV to PDF):**
- Aura converted the file herself, with 7 shell commands and about 160,000 characters of output, into OpenClaw's outbound media folder.
- Her first reply relied on OpenClaw sending it, which RMP bypasses. The evaluator answered "rework: the PDF was not successfully attached".
- She then had Claude copy the PDF into a scratch session (byte-for-byte, matching SHA-256), attached it, and the evaluator accepted. RMP sent it into Kirill's DM (8.8 MB) with no pull request.

**Kirill's decision:** Aura decides job by job whether to use Claude; her notes must tell her what Claude does and what each choice costs.

**What changed:**
- `attach_file` also takes files from `/root/.openclaw/media/outbound` (`outbox.OUTBOUND_DIR`, under OpenClaw's home), with the same checks.
- Her notes replace "Leave the work to Claude" with "When to use Claude", a guide to the choice. Repository work and diffs still go to Claude.
