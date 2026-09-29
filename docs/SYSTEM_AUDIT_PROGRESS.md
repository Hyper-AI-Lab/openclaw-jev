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

---

## Step 3 — Nothing unjudged reaches Slack (B1, B4)

**Date:** 2026-09-29 (18:20–18:40 CEST)

**What changed:**

- `app/reconciler.py`: orphan recovery acts only when Temporal reports the run closed or missing (an unreachable Temporal means "decide next pass"). A generic task restarts `GenericTaskWorkflow` under the same id from the run's original input (read from its history: tags, brief with RECENT DIALOGUE, attempt policy), plus `recovered_draft`; without history the input is rebuilt from the task row. A catalog run is closed as failed with a notice, since replaying it could repeat side effects. The reconciler no longer posts to Slack or terminates the run; events `reconciler.orphan_reply_rejudged` / `reconciler.orphan_run_failed`. `notify_slack_user_safe` removed (no callers).
- `app/workflows/generic_task.py`: judgment moved from `_plan_driven_loop` into `_judge_and_deliver`, which leaves only by acceptance or `_escalate`; a rework limit below the escalation attempt now escalates after judging the last attempt instead of sending an unjudged rework. Canaries still get no rework, but a failed evidence check now fails them instead of passing silently. `run()` accepts `recovered_draft` and judges it; escalation marks `user_notified`, so `run()` no longer adds a second "couldn't complete" message. The attempt policy is built once. Workflow imports moved to module level (the sandbox warned about in-method imports).
- `app/activities/openclaw_activities.py`: evaluator calls pass `task_id`, so task liveness is refreshed while the evaluator runs.

**Verification:**

- First real workflow tests (Temporal time-skipping server, stubbed activities): a recovered draft is judged before delivery; a rejected draft is reworked and only the accepted rework is sent; limit 2 with escalation at 20 judges two attempts, never produces the third draft and sends only the diagnosis; escalation sends exactly one message. They pass with warnings as errors.
- Reconciler tests: running run untouched; dead generic run restarted from its input with the draft; missing run restarted from the task row; dead catalog run closed with a notice, not replayed; prior recovery or unreachable Temporal does nothing.
- Source tests updated to the new method names (same assertions). Full suite 518 passed, 3 skipped; node 11 passed. Undefined-name check on edited functions: 0.
- Live: 0 running workflows at deploy; reload 18:35:45; health canary `CANARY OK` through `_judge_and_deliver`.

**Observed during verification (assigned to later steps, not fixed here):**

- `update_task_status` runs registry indexing (embedding + Qdrant upsert) inside a 10 s activity; the canary's first attempt timed out after committing and was retried. Assigned to Step 9 (index through the outbox).
- The task registry intake searches holds 3,125 canary entries vs 140 user entries; intake cited "prior finished gateway/memory canary tasks" on Sep 28. Same contamination as C1, in a second store. Assigned to Step 5 (exclusion + approved purge).
- Other workflow modules still import inside methods (sandbox warning for `app.notification_policy`). Assigned to Step 12 cleanup.
- Delivery resolves any session without a Slack user to the owner's DM, so synthetic user-path tests would message Kirill; user-path live proof stays with the Step 12 acceptance DMs.

---

## Step 4 — Every attached message is addressed; several targets (B3, decision 1)

**Date:** 2026-09-29 (18:40–19:02 CEST)

**What changed:**

- `app/workflows/user_messages.py` (new `AttachedMessages` mixin for both task workflows): take pending messages (stop and cancel entries stay for stop handling); fold them into Aura's current draft (`send_to_openclaw`, same session, max thinking for user tasks); after the run, wait for handlers and resubmit anything left.
- `app/workflows/generic_task.py`: each judgment round first honours stop, then folds in pending messages; stop is also honoured right after the evaluator returns (no wasted rework or delivery after "stop"); an accepted draft that predates new messages is folded and judged again; the attempt counter now counts reworks only. Completion, escalation and stop all resubmit leftovers.
- `app/workflows/catalog_task.py`: body moved to `_run`; `run` resubmits leftovers on every exit; fold-in before the first judgment and before each re-judge; leftovers also resubmitted before a durable leg continues as new. A message arriving during the catalog's final judgment is resubmitted as a follow-up rather than folded into that reply.
- `app/activities/intake_activities.py` + `worker.py`: `resubmit_user_messages` posts leftovers to `POST /tasks` (deterministic idempotency key, full intake path).
- Intake, several targets: `app/decisions/intake.py` asks Jev a yes/no `adds_to_Rn` question per running task when there are two or more; extra targets need the strict attach bar and only for `add_instructions`. `intake_prompt.py` documents `target_task_ids`. `apply_intake_policy` keeps only active, same-conversation (or durable) extra targets. The handler records the message, catch-up and `intake.attach` event on each target; `POST /tasks` signals each, acknowledges the delivered ones once ("Got it: adding “…” to the task(s) I'm working on (ids)"), and starts the message as new only if none were live.

**Verification:**

- Workflow tests (time-skipping server): a message sent while the evaluator runs is folded into the reply, and the revised reply is judged and sent; a message sent during a rework is folded in before the next judgment; a message sent after the reply (during the final status update) is resubmitted to intake; stop while judging stops with no rework.
- Intake tests: per-task questions only with several running tasks; Jev attaches both tasks at 0.97 and only one when the second is at 0.8; status and restart stay single-target; the policy drops an inactive and a cross-conversation extra target; the handler records and signals each target; acknowledgements quote the message and name the tasks. The Jev eval harness test answers the new questions "no".
- Full suite 528 passed, 3 skipped; node 11 passed; undefined-name check 0.
- Live after reload (18:59:26): 0 running workflows at deploy; health canary `CANARY OK`; intake preview: Jev consulted (277 ms) and abstained below its execution-mode bar, the LLM decided — the intended cascade.
- Multi-target and mid-task messages from Kirill are part of the Step 12 acceptance DMs.

**Note:** parallel tool calls once raced on the same test file and dropped an appended test; it was re-added and runs (8 workflow tests).

---

## Step 5 — Memory hygiene (C1, C2, C4 + registry contamination)

**Date:** 2026-09-29 (19:02–19:22 CEST)

**What changed:**

- `app/memory/promotion.py`: `promote_completion_memory` looks up the task and process run (best effort; a lookup failure no longer fails promotion) and skips canary, system and heartbeat work entirely. Procedural memory is a procedure summary — task, plan steps, tools Aura used (with failed-call count), short result — written only when tools or more than one step ran; replies stay episodic. Facts are pinned only when Jev promotion runs in `enforce` and accepted them; confidence ≥ 85 alone no longer pins.
- `app/openclaw_sessions.py`: `task_action_trace(task_id, since_ms=None)` pairs Aura's tool calls with their results from the transcript, clipped and redacted (also used by Step 6).
- `app/task_registry/indexer.py`: internal tasks are not indexed into the registry intake searches.
- `ops/purge_internal_memory.py` (run as `python -m ops.purge_internal_memory [--apply]`): dry run by default; `--apply` writes a JSON backup, deletes user/procedural rows whose source task is internal, registry entries of internal tasks and their vectors, then sweeps both shared Qdrant indexes for internal points no row references. Idempotent.
- `tests/conftest.py`: `DATABASE_URL` points at an empty temp SQLite, so no test can reach the live Postgres.

**Purge (approved), 2026-09-29 17:12 UTC:** backup `data/backups/purge-internal-memory-20260929T171211Z.json`. Verified first: all 49 user facts came from canaries (28 memory, 21 health); the 90 non-canary-typed registry entries were heartbeat prompts, mislabelled canaries and intake smoke tests. Deleted 664 memory rows (615 procedural, 49 user) and 3,215 registry entries; vectors: 98 memory + 3,031 registry by reference, then 563 memory and 184 registry points swept by content (their references were stale or missing). Legacy 4096-dim collections keep their canary points until Step 9 retires them.

**Verification:**

- Tests: no procedure for a reply without steps or tools; procedure text names steps, tools and failures; canary promotion writes nothing; user promotion stores facts and the procedure, not the reply, and pins only under Jev enforce; internal tasks are not indexed; the trace pairs calls with results, redacts a bearer token and honours `since_ms`. Full suite 536 passed, 3 skipped.
- Live: memory for a brand-new user task (3 queries): 0 canary items. Registry evidence for 3 queries: 0 internal rows (remaining canary mentions are Kirill's own messages about health checks and one old `[cron:SupersedeCanary]` test seed that the classifier counts as cron work). Health canary `CANARY OK`; memory canary `CANARY OK (memory_ok=1, prompt_ok=1)`; after both, no new user or procedural rows and no registry entries.

---

## Step 6 — The evaluator judges the work (B9, B5)

**Date:** 2026-09-29 (19:22–19:38 CEST)

**What changed:**

- `app/activities/openclaw_activities.py` `verify_response_quality`: adds Aura's tool trace (`task_action_trace`, redacted) and the process artifacts to the evaluator payload. `app/orchestrator/process_evaluator.py`: `format_action_trace` / `format_artifacts`; the prompt now requires each claim of work (read, searched, checked, ran, sent, created, updated, fixed) to match a successful action, and says knowledge-only answers need no tools.
- `app/workflows/judgment.py` (new `EvaluatorRetry` mixin, used by both task workflows): when the evaluator produces no verdict (bad JSON, model error, crashed activity) it is retried on durable timers (1, 2, 4, 8, 15, 30, 60 min, about two hours) instead of reworking Aura; after the second failure Kirill is told once that the reply is held for review; a stop interrupts the wait; if the reviewer never recovers the task is closed (`evaluator.unavailable`) with a message that no unchecked answer was sent. `catalog_task.py` uses it at both judgment sites (`_judge_or_close`).
- `app/workflows/user_messages.py`: taking pending messages also takes `_catchup_chunks` (the catalog parks messages there for a next step that may never come), so they are folded in or resubmitted.
- **Found during live verification and fixed in this step:** the reply poll accepted the latest terminal assistant message newer than dispatch time minus 5 s, so a second dispatch to the same session within 5 s of a reply returned the earlier reply. Live, the evaluator judged an invented claim "rework" but the activity returned the previous turn's "accept". Every dispatch attempt now leads its message with a unique `[RMP_DISPATCH <nonce>]` marker and both poll paths accept only replies after the user turn carrying it.

**Verification:**

- Workflow tests: evaluator error, crash and error, then accept → no Aura rework, one held notice, reply delivered after acceptance; reviewer never recovers → task closed as `evaluator_unavailable`, draft never sent, one failure finalization.
- Unit tests: trace and artifact formatting; the prompt carries the trace and the claim rule; `verify_response_quality` sends trace and artifacts to the evaluator; a reply to the previous turn inside the 5 s window is ignored and the new turn's verdict is returned.
- Full suite 542 passed, 3 skipped; node 11 passed; undefined-name check 0.
- Live on gpt-6-luna (medium) with a real trace (`read USER.md`): honest answer → accept ("The successful USER.md read confirms…"); same answer plus "I also emailed him the result and updated his calendar" → rework ("Remove the unsupported email and calendar claims"). Health canary `CANARY OK`; intake LLM path 8 s (target 45 s).
