# System audit and hardening — progress log

Append-only. One entry per plan step, newest at the bottom. Each entry states what was done, the evidence, the files changed and how it was verified.

---

## Step 1 — Switch to gpt-6-luna (Kirill's choice: finish and deploy before the audit)

**Date:** 2026-09-29 (17:09–17:30 CEST)  
**Decisions (Kirill):** gpt-6-luna everywhere; thinking `max` for Aura's task work, `medium` for intake, the Process Evaluator and canaries; only OpenAI calls at `max` get 120 s for the first byte and 30 s between chunks.

**Findings before the change (live probes on this account):**

- `gpt-6-luna` is listed for the key. Chat Completions rejects function tools with any reasoning effort (HTTP 400) and has no `max` (efforts there: none…xhigh). The Responses API takes tools at `max`.
- Real intake prompt, direct API: none 10.4 s, low 6.9 s, medium 5.9 s, high 9.0 s, xhigh 14.5 s, max 35.6 s (4,311 reasoning tokens, $0.0027). Same decision from medium up.
- Streaming at max: one run sent nothing for 69 s (total 88 s); another streamed at once with 4.5–5.0 s gaps. Medium: 4 s, gaps ≤ 2.5 s.

**What changed:**

- `app/llm/model_policy.py`: primary and subagent model `openai/gpt-6-luna`; OpenAI provider `api: openai-responses`; model row with cost, `contextWindow` 272k (the long-context price tier starts above 272k input tokens), `thinkingLevelMap` {xhigh, max} and `compat.supportedReasoningEfforts`; `thinkingDefault` and alias default `medium`; `gpt-5-nano` retired (alias and row dropped; OpenAI shuts it down 2026-12-11).
- `app/activities/openclaw_activities.py`: `_dispatch_openclaw_session(thinking=…)` sends the hook's `thinking`; `send_to_openclaw` passes `max` for user tasks and `medium` for canary/internal tasks. `app/workflows/generic_task.py`: rework dispatch carries `task_type` and `tags`, so a canary rework stays at medium.
- `patch_openclaw.sh` Patch 6d (`RMP_OPENAI_MAX_EFFORT_120S`): OpenAI calls with `options.reasoning === "max"` wait up to 120 s for the first byte (idle wrapper and provider first-event guard) and 30 s between chunks.
- `patch_openclaw.sh` Patch 6e (`RMP_GPT6_THINKING_BACKPORT`): the first live max run showed `thinking_level_change: high`. OpenClaw 2026.9.1's OpenAI thinking policy offers `max` only to `gpt-5.6*`. Backported 2026.9.6's GPT-6 branch (levels from the declared efforts); it skips itself on releases that ship `OPENAI_GPT_6_MODEL_IDS`.
- Live config: `openclaw.json` via `apply_openclaw_policy`; `settings.json` `intake_model` → `openai/gpt-6-luna`. Backups: `/root/.openclaw/{openclaw,settings}.json.bak-luna-20260929-170939`.
- Docs and rules: both `rmp-architecture.mdc` copies (identical), `CONCEPT_TREE.md` (MUST, §8, contradiction register), `ARCHITECTURE.md`, `README.md`, `settings.example.json`, readiness text, verify/upgrade scripts.

**Verification:**

- Offline: 500 Python tests pass (the one live-settings test passes after rollout), 11 node tests pass. New tests: thinking per task kind; rework carries tags; policy row and retirement. One-off undefined-name check on the edited functions: 0 (it caught an earlier `payload` scope slip before commit).
- Candidate config validated with `OPENCLAW_CONFIG_PATH=… openclaw config validate` before going live; live config valid; `ops/verify_openclaw_patch.sh` passed (primary and subagents gpt-6-luna, both new markers).
- Patch 6e offline import test: gpt-6-luna gets off…max with default medium; gpt-5-nano and gpt-5.6-terra unchanged; idempotent.
- Live max-effort Aura turn (scratch `rmp_task_lunacheck_*`, `deliver: false`): `thinking_level_change: max`, `read` tool call, correct answer, 27 s, no idle cuts.
- Same intake prompt through OpenClaw: high 315 reasoning tokens, max 1,001. The stored OpenAI responses report `reasoning.effort` `high` and `max` respectively.
- Intake LLM-path canary: PASS, 18 s (target 45 s), gpt-6-luna at medium. Health canary: `CANARY OK`, task session gpt-6-luna at medium.
- Process Evaluator prompt on gpt-6-luna at medium: correct draft → accept (6.6 s); off-topic draft → rework with a concrete `command_to_aura` (6.5 s).

**Incidents and notes:**

- The idle-aware code watcher reloaded `rmp-api`/`rmp-worker` about 3 minutes after the first `model_policy.py` edit, before OpenClaw allowed gpt-6-luna. Edits moved to a git worktree and the live tree was restored at 16:09:43. Only one run fell in the window, the hourly canary, which ran on gpt-5-nano and passed. Task runs follow `openclaw.json`'s primary, so only evaluator calls could have been affected.
- The Responses API path sends `store: true`: OpenAI keeps response objects (prompts and replies) for its retention window, which Chat Completions did not. OpenClaw uses the stored reasoning items across turns. Open item for the audit plan.
- OpenClaw 2026.9.6 (2026-09-23) supports GPT-6 natively. Upgrading would retire Patch 6e. Open item for the audit plan.
- Scratch sessions left in the OpenClaw store: `rmp_task_lunacheck_*`, `rmp_task_lunaeffort_*`, `rmp_verify_lunacheck_*` (no RMP task rows, `deliver: false`).

---

## Audit (read-only) — findings that define Steps 2–12

**Date:** 2026-09-29. **Method:** 30 days of Postgres rows, OpenClaw transcripts, service, canary and plugin logs, code, and web research for the fix designs (Temporal message handling, Slack message identity, agent-as-a-judge trajectory evaluation, Postgres→Qdrant outbox). Grok subagents were unavailable (provider usage limit until 2026-10-01 00:00 UTC), so the audit was done directly.

**Plan:** `/root/.cursor/plans/system_audit_hardening.plan.md`, with the evidence for each finding.

- P0: settings rewritten on every read, non-atomically; a race deleted the `jev` section today, so Jev intake and promotion are silently off (D1). The reconciler delivers unjudged replies (10 of 23 user deliveries in 30 days) and can kill live workflows (B1). Attached messages that arrive during the last step, evaluation or rework are never addressed (B3). Canary replies are stored as user facts and appear in every user-task prompt (C1).
- P1: last rework draft delivered unjudged if limits are misconfigured (B4); evaluator blind to Aura's actions (B9); evaluator outages rework Aura (B5); Slack failures lose replies (B6); replies truncated (B7); text-hash dedupe swallows new identical DMs (A1); thread/reply context dropped (A2); attachment-only DMs dropped (A3); cron can clarify (A12); no Postgres↔Qdrant reconciliation, 432 rows unindexed (C3); API key in a world-readable log (A7); raw replies as "procedural" memory (C2); no monitor for any of this (M1); no real workflow tests (T1).
- P2: `[object Object]` errors, timeout statuses, stale cron path, strategy-change cadence, stray `.bak`, model catalog (A8, A11, A13, B8, A4, A5).
- Healthy: every DM claimed; Sep 27 self-attach fixed same day; Jev-era decisions sensible; related-task context; suppressed deliveries all canaries; no Slack API errors; catalog gate sound; canaries 24/24 per day since Sep 16.

**Kirill's decisions (2026-09-29):** plan approved, execute Steps 2–12 in order; attach a message to every running task it clearly relates to; turn OpenAI response storage off if OpenClaw can carry reasoning without it (verify first); approved: rotate the RMP API key and purge the old plugin log, delete canary-derived memory rows and vectors, add DB tables/columns, retire the three legacy Qdrant collections after verification, restore the `jev` section.

---

## Step 2 — Settings integrity (D1)

**Date:** 2026-09-29 (18:15–18:20 CEST)

**Root cause, reproduced:** the old `load_settings()` rewrote `settings.json` on every call from every process, with truncate-then-write; `_read_json` turned a half-written file into `{}` and the defaults were written back. Running the old code with 4 readers and 2 writers on a temp file lost the `jev` section in 1,193–1,198 of 1,200 reads (3 runs), and the final file had no `jev` each time. That is how Jev intake (enforce) and promotion (shadow) were switched off on 2026-09-29 between 15:36 and 17:09 CEST. Local test runs also wrote the live file (no `conftest.py`), which is likely what raced with the services.

**What changed:**

- `app/config.py`: `load_settings()` is read-only (defaults merge in memory). `update_settings(mutate)` is the one writer: `flock` on `settings.json.lock`, temp file, `fsync`, `os.replace`, file mode kept; `mutate` edits only stored keys, so defaults are never baked in. A corrupt file raises `SettingsCorruptError` instead of reading as empty. The API key is created once under the lock when missing; `RMP_API_KEY` from the environment is used but no longer copied into the file. `save_settings` and `_write_json` removed (no callers left).
- `app/api/server.py`: `POST /settings` and `POST /dev/suspend-all` write through `update_settings`.
- `tests/conftest.py`: tests get temp `OPENCLAW_HOME`, `RMP_DATA_DIR` and `RMP_SETTINGS_PATH` before `app` is imported, as CI sets them.
- `app/production/readiness.py`: `settings_integrity` fails when the file is corrupt, the `jev` section is missing or invalid, or the stored API key differs from `RMP_API_KEY` (plugin and API would disagree).
- Live: `jev` restored from backup `20260929T031503Z` through `update_settings` (intake `enforce`, promotion `shadow`, thresholds 0.85/0.92/0.95/0.95).

**Verification:**

- New tests: reads leave bytes and mtime unchanged; unknown keys kept, defaults not stored; corrupt file raises; env key not written; API key created once across 6 processes; 4 writers × 40 updates with 4 readers × 300 reads: no lost key, every counter exact, no temp files left. Readiness: pass, missing, invalid, corrupt and key-mismatch cases.
- Full suite hermetic: 509 passed, 3 skipped.
- Live after reload (18:16): `settings.json` untouched for 75 s while services ran; `get_policy()` = enforce/shadow; `settings_integrity` pass; intake previews answered by Jev (`decision_source: jev`, 254 ms).
