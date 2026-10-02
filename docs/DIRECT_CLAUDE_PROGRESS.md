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
