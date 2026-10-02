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
