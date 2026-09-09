# Analyst control-plane progress log

Append-only. Each completed plan step adds a dated entry below. Do not rewrite prior entries.

---

## 2026-08-13 — Step 1: Freeze the control-plane contract

- **Status:** complete (docs only; no runtime behavior change)
- **Files changed:**
  - `ARCHITECTURE.md` — §3.1 message flow; new §5.0.0 Analyst control plane (roles, four relation classes, always-gated Slack, attempt 10/20, hybrid retrieval as evidence); §5.0.1 and ASCII overview aligned
  - `/root/.openclaw/workspace/TOOLS.md` — Aura-visible roles, PROCESS BRIEF, always-gated path
  - `docs/CONTROL_PLANE_PROGRESS.md` — this log created
- **Verification:** `rg` on ARCHITECTURE.md and TOOLS.md confirms Intake Analyst, Process Evaluator, four relation classes, always-gated delivery, attempt 10/20
- **Remaining risk:** runtime still uses the old surrogate until Steps 2–10 land

---

## 2026-08-13 — Step 2: Hybrid retrieval service

- **Status:** complete
- **Files changed:**
  - `app/task_registry/hybrid_retriever.py` — RRF (k=60), Postgres FTS, user-memory search, Temporal liveness, status boosts, fail-soft fusion
  - `app/task_registry/retriever.py` — host-wide active tasks; `hybrid_search_bounded` attaches `evidence_pack` / `memory_hits` / `fts_hits` when deadline ≥ 3s
  - `app/db/database.py` — GIN tsvector indexes on tasks.goal, registry intent+outcome, task_messages.content
  - `tests/test_hybrid_retriever.py` — RRF both-list preference, FTS+dense fusion, fail-soft pack
- **Verification:** `pytest -q tests/test_hybrid_retriever.py tests/test_intake_bounded_context.py` → 8 passed
- **Remaining risk:** FTS indexes apply on next `init_db` / RMP restart (Step 10)

---

## 2026-08-13 — Step 3: Intake Analyst evidence pack; vector gate advisory-only

- **Status:** complete
- **Files changed:**
  - `app/task_registry/vector_gate.py` — always returns None; stashes `advisory_hits`
  - `app/task_registry/intake_context.py` — evidence_pack / memory_hits / fts_hits; intent cap 20000
  - `app/task_registry/intake_prompt.py` — four relation classes, clarify, evidence in payload
  - `app/api/server.py` — POST /tasks and intake preview use full `raw_text`
  - `plugins/rmp_adapter/index.js` (+ live `/root/.openclaw/plugins/rmp_adapter/index.js`) — drop 500-char intent truncation
  - `tests/test_vector_gate.py`, `tests/test_intake_evidence.py`
- **Verification:** `./venv/bin/pytest -q tests/test_vector_gate.py tests/test_intake_evidence.py tests/test_intake_catalog_adjudication.py tests/test_intake_bounded_context.py tests/test_hybrid_retriever.py tests/test_intake_deterministic.py` → 24 passed
- **Remaining risk:** `clarify` / `rebuild_stale` are in the prompt schema but handlers/policy still normalize unknown decisions to `create_fresh` until Step 4

---

## 2026-08-13 — Step 4: Analyst decisions (clarify / rebuild / no silent drop)

- **Status:** complete
- **Files changed:**
  - `app/task_registry/intake_decision_engine.py` — `clarify`/`rebuild_stale`; low-confidence → clarify (or wait if one clear active); canaries stay `create_fresh`; pending-clarify follow-up attaches
  - `app/task_registry/intake_handlers.py` — Slack question on clarify (no Temporal); wait/skip user-visible ack; attach catch-up signal; rebuild terminates then falls through with process memory
  - `app/api/server.py` — `clarify` returns without workflow; `resume_clarify` starts on existing task; attach uses `signal_text`
  - `app/task_registry/retriever.py` — `intake_clarify` flag on active rows
  - `app/task_registry/intake_prompt.py` — parse failure is `clarify`
  - `app/reconciler.py` — one clarify reminder then wait
  - `plugins/rmp_adapter/index.js` (+ live copy) — log/return clarify + resume
  - `tests/test_intake_handlers.py`, `tests/test_task_intake_extended.py`
- **Verification:** `./venv/bin/pytest -q tests/test_intake_handlers.py tests/test_task_intake.py tests/test_task_intake_extended.py tests/test_intake_catalog_adjudication.py tests/test_intake_deterministic.py tests/test_execution_mode.py` → 39 passed
- **Remaining risk:** PROCESS BRIEF is a text block today; Step 5 must inject it into catalog child prompts (catalog currently drops `initial_memory_block`)

---

## 2026-08-13 — Step 5: PROCESS BRIEF on generic and catalog paths

- **Status:** complete
- **Files changed:**
  - `app/orchestrator/process_brief.py` — compose brief + fetched memory; user catch-up formatter
  - `app/workflows/catalog_task.py` — read `initial_memory_block`; prepend to every child `memory_block`; persist brief; attach signals no longer dropped by `_consume_stop`
  - `app/workflows/generic_task.py` — always compose brief + process memory; drain attach catch-up into step context
  - `tests/test_process_brief.py`
- **Verification:** `./venv/bin/pytest -q tests/test_process_brief.py tests/test_memory_daily_use.py tests/test_prompt_policy_timezone.py tests/test_catalog.py tests/test_intake_handlers.py` → 46 passed (plus new catalog-source assertion)
- **Remaining risk:** conversational generic still Slack-first without Process Evaluator until Step 6

---

## 2026-08-13 — Step 6: Mandatory Process Evaluator (fail closed)

- **Status:** complete
- **Files changed:**
  - `app/orchestrator/process_evaluator.py` — verdict parse (`accept|rework|strategy_change|escalate_user`); malformed JSON → rework, never accept; persist `evaluator.verdict`
  - `app/activities/openclaw_activities.py` — `verify_response_quality` uses evaluator prompt on `rmp_verify_*`
  - `app/orchestrator/decision_engine.py` — missing quality without skip → retry
  - `app/workflows/generic_task.py` — conversational no longer Slack-before-gate
  - `app/workflows/catalog_task.py` — catalog/artifact evidence miss reworks instead of instant-fail; always judge
  - `app/orchestrator/completion_rework.py` — evaluator command in rework prompt
  - `tests/test_process_evaluator.py`, `tests/test_generic_task_conversational.py`
- **Verification:** `./venv/bin/pytest -q tests/test_process_evaluator.py tests/test_generic_task_conversational.py tests/test_vision_completion.py tests/test_evidence.py tests/test_process_brief.py` → 36 passed
- **Remaining risk:** attempt cap still 3 until Step 7 (10/20)

---

## 2026-08-13 — Step 7: Attempt law 10 / 20

- **Status:** complete
- **Files changed:**
  - `app/orchestrator/completion_rework.py` — `next_loop_action`, strategy-change prompt, escalation diagnosis; `should_admit_failure` no longer auto-fires at max (off-by-one)
  - `settings.json` / `settings.example.json` / `app/config.py` — `rework_max_attempts=20`, `strategy_change_attempt=10`, `escalate_user_attempt=20`
  - `app/workflows/generic_task.py`, `app/workflows/catalog_task.py` — honor 10/20; canary `max_rework=0`
  - `tests/test_attempt_law.py`, `tests/test_task_intake_extended.py`
- **Verification:** `./venv/bin/pytest -q tests/test_attempt_law.py tests/test_task_intake_extended.py tests/test_process_evaluator.py tests/test_generic_task_conversational.py tests/test_vision_completion.py` → 35 passed
- **Remaining risk:** a 20-turn MiniMax loop is expensive / rate-limit sensitive (noted again in Step 10)

---

## 2026-08-13 — Step 8: Evaluator situational tools

- **Status:** complete
- **Files changed:**
  - `app/orchestrator/evaluator_tools.py` — allow health/readiness/web_capability_status/web_search/jina_reader; deny systemctl/filesystem/secrets; localhost-only HTTP
  - `app/activities/openclaw_activities.py` — fetch situational context before judge
  - `app/orchestrator/process_evaluator.py` — prompt includes SITUATIONAL TOOLS
  - `tests/test_evaluator_tools.py`
- **Verification:** `./venv/bin/pytest -q tests/test_evaluator_tools.py tests/test_process_evaluator.py tests/test_attempt_law.py tests/test_completion_rework.py` → 26 passed
- **Remaining risk:** optional web_search/jina hit local :8791 and fail-soft if the galaxy stack is down

---

## 2026-08-13 — Step 9: Ledger completeness

- **Status:** complete
- **Files changed:**
  - `app/orchestrator/process_evaluator.py` — persist `evaluator.verdict` plus `evaluator.accept`/`evaluator.escalate`; evaluator `task_messages`
  - `app/activities/side_effects.py` — `slack.delivered` event + assistant `task_messages`
  - `app/task_registry/summary.py` — index intake/evaluator/slack events into `outcome_summary`
  - `tests/test_ledger_control_plane.py`
- **Verification:** `./venv/bin/pytest -q tests/test_ledger_control_plane.py tests/test_intake_handlers.py tests/test_export.py` → 18 passed
- **Remaining risk:** Slack ledger write is fail-soft if DB commit races with the receipt session

---

## 2026-08-13 — Step 10: Production verification and safe reload

- **Status:** complete
- **Files changed:**
  - `app/workflows/generic_task.py`, `app/workflows/catalog_task.py`, `app/temporal_control.py` — attempt policy is payload-only (no `os.environ` / settings reads inside Temporal workflow sandbox)
- **Verification:**
  - Focused pytest (retrieval, intake, evaluator, 10/20, catalog brief, ledger): 102 passed
  - Active user tasks: 0 → `ops/restart_rmp.sh` → health OK, boot stamps OK
  - `GET /health` status=ok; `GET /api/workflow-catalog` templates=7
  - Canary: first post-restart run compensated (`os.environ` in workflow — fixed); second run timed out on NVIDIA 429 key rotation during `send_to_openclaw` (not a control-plane gate bug). `RMP_CANARY_SKIP_SENTINEL=1`
- **Remaining operational risks:**
  - 20-turn MiniMax evaluator/rework loops are expensive and will hit NVIDIA rate limits
  - Health canary is still 1-step but still needs a free LLM slot; 429s can make hourly canary time out
  - FTS GIN indexes apply on `init_db` (restart performed)
  - Galaxy `:8791` situational tools fail-soft if the web stack is down

---

## 2026-08-13 — Step 10 follow-up: health canary re-run

- **Status:** complete
- **Verification:**
  - Active user tasks: 0; NVIDIA keys off cooldown (`nvidia:key2` available)
  - `GET /health` status=ok; `GET /api/workflow-catalog` templates=7
  - `RMP_CANARY_SKIP_SENTINEL=1 bash /root/.openclaw/rmp/ops/canary.sh` → **CANARY OK**
  - Task `0704e510-1491-4f72-b045-8a4a95cda22a` completed (~35s; poll 3)
  - `data/last_health_canary.json` status=completed at 2026-08-13T04:20:07Z
- **Remaining operational risks:** unchanged (20-turn MiniMax loops, hourly 429 timeouts, Galaxy `:8791` fail-soft)

---

## 2026-09-05 — OpenClaw 2026.9.1 upgrade + RMP patches

- **Status:** complete
- **What landed:**
  - Global OpenClaw `2026.7.1-2` → **`2026.9.1`**. Backup: `data/backups/openclaw-update-20260904T235808Z/`
  - `OPENCLAW_SERVICE_REPAIR_POLICY=external openclaw doctor --fix --non-interactive` (no `--force`; this host uses a system unit)
  - Dist patches re-applied for 2026.9 symbols; new **session canonical** + **updatedAt drift** patches so `/hooks/agent` is not fail-closed by `session_nodes.entry_valid` / timestamp lag
  - Repeatable path: `ops/upgrade_openclaw.sh`, `make upgrade-openclaw`, `.cursor/rules/openclaw-upgrade.mdc`
  - Session store settlement: `ops/settle_openclaw_sessions.py` (never drop `entry_valid` triggers — gateway refuses to boot without them)
  - Auth stays in SQLite `authProfiles.store`; leftover JSON must not be recreated
- **Files changed (this follow-through):**
  - `patch_openclaw.sh`, `ops/verify_openclaw_patch.sh`, `ops/upgrade_openclaw.sh`, `ops/settle_openclaw_sessions.py`
  - `tests/test_settle_openclaw_sessions.py`
  - `ARCHITECTURE.md` §4.0/§4.1, `.cursor/rules/openclaw-upgrade.mdc`
- **Verification:**
  - `./venv/bin/pytest -q tests/test_settle_openclaw_sessions.py tests/test_openclaw_sessions.py tests/test_quota_broker.py` → 9 passed
  - `ops/verify_openclaw_patch.sh` passed (including llm-idle-5s, session placeholder skip, timestamp drift)
  - Active user tasks: 0 → `ops/restart_rmp.sh` → health OK; Slack socket connected
  - `make production-check` → intake execution_mode PASS; intake latency **38s**, confidence **97**
  - `RMP_CANARY_SKIP_SENTINEL=1 bash ops/canary.sh` → **CANARY OK** task `e0bebe59-05d9-4c28-9760-5e45c5d50d3c` at 2026-09-05T01:09:53Z
- **Remaining operational risks:**
  - NVIDIA 429s and DeepSeek TTFT can exceed the 5s LLM idle patch; GLM-5.2 / some embed models are HTTP 410 EOL
  - Slack socket mode reports 2 active connections for this app (duplicate gateway/relay)
  - `skills/moltmarket` is a symlink; 2026.9 logs `Skipping escaped skill path`
  - Do not recreate `auth-profiles.json` beside the SQLite store

---

## 2026-09-05 — Keys module + OmniRoute decision (log only)

- **Status:** decision logged; no behavior change in this step
- **Current state:** RMP `quota_broker` is NVIDIA-only (env `NVIDIA_API_KEY*` → SQLite `authProfiles.store`, cooldowns, concurrency). OpenClaw primary is MiniMax M3 with DeepSeek then GLM fallbacks. GLM-5.2 is HTTP 410 EOL on NVIDIA.
- **OmniRoute ([diegosouzapw/OmniRoute](https://github.com/diegosouzapw/OmniRoute)):** rejected as an in-path sidecar for Aura Slack. A second always-on Node gateway next to OpenClaw 2026.9.1 adds crash loops and another Slack-adjacent surface; default `auto` / free-tier combos can send user DMs to unknown models (violates RMP model policy); compression (RTK/Caveman) can break RMP JSON intake and tool calls. OpenClaw already does provider fallback; RMP already rotates keys.
- **Take from OmniRoute (implement in RMP keys module, not a sidecar):** ordered combo, treat HTTP 410 as skip (not a 5s idle retry), multi-key cooldown.
  - **Target combo:** `openai/gpt-5-nano` → `nvidia/minimaxai/minimax-m3` → `nvidia/deepseek-ai/deepseek-v4-flash-0731` (drop GLM). OpenAI Platform key in `/etc/openclaw/openclaw.env` as `OPENAI_API_KEY` (never git, never Slack, never `openclaw.json`). NVIDIA keys stay `NVIDIA_API_KEY`, `_2`, `_3` in the same file.

---

## 2026-09-05 — Keys module (model_policy + env sync)

- **Status:** complete (behavior lands with OpenClaw/RMP consume steps)
- **What landed:**
  - `app/llm/model_policy.py` — canonical primary/fallbacks; `apply_openclaw_policy()` writes OpenClaw model + allowlist + openai row + `auth.order`
  - `quota_broker.sync_llm_auth_profiles()` loads `OPENAI_API_KEY` → SQLite `openai:default`; NVIDIA profiles unchanged; leftover JSON still retired not recreated
  - `ops/sync_nvidia_keys.py` remains the systemd ExecStartPre wrapper
  - `ops/upgrade_openclaw.sh` follows this policy (no MiniMax-as-primary / GLM restore from backup)
- **Tests added:** `tests/test_model_policy.py`; sqlite openai+nvidia sync in `test_quota_broker.py`

---

## 2026-09-05 — OpenClaw consumes gpt-5-nano policy

- **Status:** complete
- **What landed:** live `openclaw.json` primary `openai/gpt-5-nano` with `agentRuntime.id: "openclaw"`; fallbacks MiniMax then DeepSeek; GLM removed from allowlist/aliases/NVIDIA catalog; `models.providers.openai` at `https://api.openai.com/v1` (completions); `plugins.allow` includes `openai`; `auth.order.openai` = `openai:default`. Idle stays 5s. Did not run `openclaw onboard`.

---

## 2026-09-05 — RMP intake/docs consume the same policy

- **Status:** complete
- **What landed:** `settings.json` / example / `DEFAULT_TASK_REGISTRY` intake chain = gpt-5-nano → MiniMax → DeepSeek. `MODEL_CATALOG` drops GLM, adds gpt-5-nano. `assign_openclaw_session_profile` / plugin pin only when `should_pin_nvidia_profile` (never on `openai/*`). ARCHITECTURE §4 + Cursor `rmp-architecture.mdc` primary line updated.

---

## 2026-09-05 — Fail-closed 410 / missing OpenAI key

- **Status:** complete
- **What landed:** readiness check `openai_key` warns `openai_key_missing` (non-blocking). Dist patch `RMP_410_SKIP` classifies HTTP 410 as `model_not_found` not timeout. GLM removed so 410 EOL is not in the chain. `OPENAI_API_KEY` documented in ARCHITECTURE, `openclaw-upgrade.mdc`, and a comment in `/etc/openclaw/openclaw.env` (key not set yet).

---

## 2026-09-05 — Keys module verify

- **Status:** complete
- **Verification:**
  - pytest `test_model_policy` `test_quota_broker` `test_intake_models` `test_llm_orchestration` `test_readiness` → 18 passed
  - `ops/verify_openclaw_patch.sh` passed (including `RMP_410_SKIP`); primary `openai/gpt-5-nano`, fallbacks MiniMax then DeepSeek, no GLM
  - Live probe (no secrets logged): `OPENAI_API_KEY` unset → gpt-5-nano skipped (`openai_key_missing`); MiniMax NVIDIA chat completions **HTTP 200**
  - Active user tasks: 0 → restarts → health OK
  - `make production-check` → readiness **21 pass / 1 warn** (`openai_key_missing`); intake execution_mode PASS; intake latency **23s**, confidence **92** (NVIDIA MiniMax fallback)
  - `RMP_CANARY_SKIP_SENTINEL=1 bash ops/canary.sh` → **CANARY OK** task `5971fa59-e725-482d-8838-ce9243f91154` at 2026-09-05T08:41:09Z
- **Operational notes until the OpenAI key is pasted:**
  - Config primary stays `openai/gpt-5-nano` so inserting `OPENAI_API_KEY` in `/etc/openclaw/openclaw.env` then `systemctl restart openclaw-gateway` is enough
  - Intake/dispatch skip unwired `openai/*` and use MiniMax (OpenClaw still has DeepSeek as next fallback). `intake_llm_timeout_sec` is 40 so 5s idle retries can finish
  - Builtin OpenClaw `memory.search` is disabled so adding the openai chat provider does not default embeddings to `text-embedding-3-small`
- **Remaining:** paste `OPENAI_API_KEY` in `/etc/openclaw/openclaw.env` and restart the gateway to wire gpt-5-nano

---

## 2026-09-05 — Health canary timeout after OpenAI key paste

- **Status:** complete (incident cleared)
- **Trigger:** Slack `health_canary` / `scheduled` timeout on tasks `9dd07fcc` then recovery `b51b28f3`. Gateway PID still predated the paste, so process env lacked `OPENAI_API_KEY`. OpenClaw failed gpt-5-nano (`secret reference was not found` on `${OPENAI_API_KEY}`), then MiniMax/DeepSeek hit 5s idle.
- **What landed:** stopped writing `models.providers.openai.apiKey: ${OPENAI_API_KEY}` (SQLite `openai:default` is agent auth). Synced keys, restarted RMP+gateway with 0 active user tasks. Gateway process now has `OPENAI_API_KEY`.
- **Verification:**
  - pytest `test_model_policy` `test_quota_broker` `test_intake_models` `test_readiness` → 16 passed
  - Live probe (no secrets): gpt-5-nano HTTP 200 TTFT 1.61s; MiniMax HTTP 200 TTFT 3.81s
  - `RMP_CANARY_SKIP_SENTINEL=1 bash ops/canary.sh` → **CANARY OK** task `3ba174fd-4fa7-4c4e-8dca-7f79fb46668f` at 2026-09-05T10:46:12Z (gpt-5-nano HTTP 200)
  - readiness **22 pass / 0 warn**; patch verify OK
- **Watch:** OpenClaw canary fetch elapsed ~6.5s with `thinking=medium, fast=off` vs 5s idle. Direct API TTFT is 1.6s. If hourly canaries time out again, set model-scoped thinking `low` — do not raise idle.
- **Remaining:** usage still records many `nvidia:unknown` turns; ARCHITECTURE still mentions leftover `auth-profiles.json` in a few ops sections.

---

## 2026-09-05 — User ping parked on health canary (`wait_active`)

- **Status:** complete
- **Trigger:** Slack "Are you here aura?" → "I'm already working on this (task 98919271)". Task `98919271` was the hourly health canary on `agent:main:main`, later `stopped_by_user`. Intake LLM cancelled (`IntakeWorkflow failed`) while the canary held OpenClaw; deterministic fallback confidence 0 then `low_confidence_wait`.
- **What landed:** user DMs ignore canary/heartbeat/system actives (`user_visible_active_tasks`). Degraded intake still `create_fresh` + Temporal `GenericTaskWorkflow` (never native Slack; `conversational` is plan shape only). Wait/attach onto an internal target is `internal_active_ignored`.
- **Verification:** pytest `test_intake_handlers` `test_intake_bounded_context` `test_execution_mode` `test_intake_workflow_runner` `test_task_intake` `test_task_intake_extended` → 41 passed
- **Remaining:** canaries still share `agent:main:main` and OpenClaw concurrency, so intake LLM can still cancel during a long canary; degraded path still starts an RMP task instead of `wait_active` on the canary. `nv-embed-v1` HTTP 410 on registry/memory search.

---

## 2026-09-05 — Ping task silent for ~9 minutes (`02622839`)

- **Status:** complete (stuck task cancelled; dispatch/poll fixed)
- **Trigger:** Slack "Are you here aura?" at 12:47Z created task `02622839` (full RMP path: intake `create_fresh` → Temporal → `rmp_task_*`). No Slack. Deliver step hung in `send_to_openclaw` (600s poll). gpt-5-nano `thinking=medium` hit 5s idle; all three models failed; poll ignored `All models failed` and also rejected short greetings (`len >= 80`).
- **What landed:** `thinkingDefault=low` and `openai/gpt-5-nano` `params.thinking=low`. Poll accepts `CANARY_OK` and short real replies. `All models failed` raises immediately. Stuck task cancelled.
- **Verification:** pytest `test_openclaw_poll` `test_model_policy` `test_evidence` → 21 passed. Gateway `agent model: openai/gpt-5-nano (thinking=low, fast=off)`. `/health` OK after restart.
- **Remaining:** hourly canary still competes for OpenClaw lanes (MiniMax/DeepSeek also 5s-idle under the same prompt). User should re-send the ping as a new RMP task.

---

## 2026-09-05 — Slack follow-ups forgot prior dialogue / CEST afternoon greeting

- **Status:** complete
- **Trigger:** Kirill DM 10:07–10:34 JST. Aura said good evening (correct JST night), then later **good afternoon** and dumped web-tool catalogs for an RMP-opinion question. Host clock is CEST; each Slack line was `create_fresh` with empty process memory and PROCESS BRIEF “start fresh.”
- **What landed:** `POST /tasks` injects **RECENT DIALOGUE** from same-session `task_messages` (not embeddings). `create_fresh` brief is “new task row, not amnesia.” Greetings locked to Asia/Tokyo (`good evening` at night; afternoon forbidden). Dialogue timestamps are shown in JST, not UTC. Conversational deliver must not list TOOLS.md crawlers unless asked.
- **Files changed:**
  - `app/task_registry/messages.py` — `format_session_dialogue` / `recent_session_dialogue_block`
  - `app/api/server.py` — join dialogue into `initial_memory_block` (skip canary/cron/heartbeat)
  - `app/task_registry/intake_handlers.py` + `intake_prompt.py` — create_fresh wording
  - `app/orchestrator/prompt_policy.py` — JST greeting lock
  - `app/activities/plan_activities.py` — conversational deliver
  - `ARCHITECTURE.md`, workspace `TOOLS.md` / `AGENTS.md`
  - tests: `test_session_dialogue.py`, timezone, process_brief, intake_handlers, web_capability
- **Verification:** pytest on the files above (this entry). Gateway not required; RMP builds the prompt.
- **Remaining:** vector memory still `nv-embed-v1` HTTP 410; dialogue is Postgres-only. Intake LLM can still cancel during canaries. Model may still ignore the time block if it does not follow instructions.

---

## 2026-09-05 — Remove incident phrase-locks (greeting / crawler bans)

- **Status:** complete
- **Trigger:** Operator: greeting-word locks and “don’t list crawlers” are static narrow-case rules, not dynamic control-plane behavior.
- **What landed:** Deleted `greeting_lock_for_period` / period buckets / forbidden-phrase text. `USER LOCAL TIME` is a clock fact only. Conversational deliver and TOOLS.md/AGENTS.md no longer ban crawlers or greetings. Kept RECENT DIALOGUE and `create_fresh` ≠ amnesia.
- **Files changed:** `prompt_policy.py`, `plan_activities.py`, `messages.py`, `ARCHITECTURE.md`, workspace `TOOLS.md` / `AGENTS.md`, tests `test_prompt_policy_timezone.py` `test_session_dialogue.py` `test_process_brief.py`
- **Verification:** pytest on those tests (this entry).
- **Remaining:** regex `GENERIC_PROFILES` and other keyword surrogates are older and out of this cleanup; operator will task that next.

---
