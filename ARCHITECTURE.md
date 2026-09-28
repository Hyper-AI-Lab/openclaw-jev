# Aura System Architecture

**Last updated:** 2026-09-05  
**Host:** Single Linux VPS (Europe/Berlin timezone on server; Kirill in JST)  
**Status:** Production live (`development_mode: false`)

This document describes the **runtime** of **Aura** — OpenClaw + RMP on this VPS. Binding constitution (why / must / conflict law): [`docs/CONCEPT_TREE.md`](docs/CONCEPT_TREE.md). Cursor agents must not violate that file.

Related logs:

- [`docs/CONCEPT_TREE.md`](docs/CONCEPT_TREE.md) — source of truth
- [`docs/CONCEPT_TREE_PROGRESS.md`](docs/CONCEPT_TREE_PROGRESS.md) — constitution work log
- [`docs/history/DEVELOPMENT_PLAN.md`](docs/history/DEVELOPMENT_PLAN.md) — Phases 0–11 (historical)
- [`docs/history/PRODUCTION_PLAN.md`](docs/history/PRODUCTION_PLAN.md) — Phases 12–20 (historical)
- [`docs/CONTROL_PLANE_PROGRESS.md`](docs/CONTROL_PLANE_PROGRESS.md) — Analyst control-plane log (append-only)
- [`docs/runbooks/slack-sockets.md`](docs/runbooks/slack-sockets.md) — this-host Slack sockets only; never `apps.connections.open`

---

## 1. Executive summary

Aura is a **three-layer system** on one machine:

| Layer | Path | Role |
|-------|------|------|
| **Safe Harbor** | `/root/aura_safe_harbor` | Legacy scripts, Moltbook scanners, watchdog (`restore_powers.js`), Kairos modules |
| **OpenClaw** | `/root/.openclaw` | Agent runtime: Slack gateway, LLM, workspace, cron, plugins |
| **RMP** (Reliability & Memory Plane) | `/root/.openclaw/rmp` | Sidecar API + Temporal workflows + Postgres ledger + vector memory + LLM orchestration |

**How the layers combine:** OpenClaw is the **runtime** (Slack socket, gpt-5-nano + NVIDIA MiniMax/DeepSeek fallbacks, tools, JSONL sessions, cron). RMP wraps it as a **sidecar control plane**: the `rmp_adapter` plugin intercepts inbound messages, creates durable tasks, and blocks the main/Slack session so work runs in isolated `rmp_task_*` sessions under Temporal. Safe Harbor supplies legacy scanners and watchdog scripts; RMP can invoke or sync them when not in development mode.

**Design intent:** User-facing work (Slack DMs, cron) is **routed through RMP** so every turn becomes a durable Temporal workflow with steps, observations, evidence checks, and idempotent Slack delivery. OpenClaw remains the **execution engine** (tools, LLM, JSONL sessions); RMP is the **control plane** — it owns plans, step predicates, process memory, completion gates, reconciliation, and **LLM key orchestration** (balanced rotation, concurrency caps, usage accounting).

**Primary LLM (chat):** OpenAI gpt-5-nano (`openai/gpt-5-nano`) on OpenClaw runtime (`agentRuntime.id: "openclaw"`), with MiniMax M3 then DeepSeek V4 Flash as NVIDIA fallbacks after auth-key rotation. **Intake** uses the same chain. GLM-5.2 is dropped (NVIDIA HTTP 410). HTTP 410 is treated as skip, not a 5s idle retry.

**Vector embeddings:** Advisory only. Live embedder is OpenAI `text-embedding-3-small` (1536-d) after NVIDIA NIM embeddings returned HTTP 410 EOL. `/health` `ready=true` only after a successful embed probe. Conversational continuity still uses Postgres `task_messages` + process memory and must not depend on embeddings.

**Rate-limit policy:** On NVIDIA **rate limits**, RMP **waits, rotates keys, and tracks usage** (does not hop providers for 429s). Separate from that, OpenClaw keeps an ordered **model fallback chain** for unavailable/broken models. OpenAI key: `OPENAI_API_KEY` in `/etc/openclaw/openclaw.env` (SQLite profile `openai:default`). Three NVIDIA accounts (`nvidia:default`, `nvidia:key2`, `nvidia:key3`) with **balanced load** and a **max concurrent agent-run cap** (default 2). Never pin `nvidia:keyN` on an `openai/*` session.

---

## 2. High-level architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Kirill (Slack DM)                                 │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │ Socket Mode
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  OpenClaw Gateway (:18789)          /root/.openclaw/openclaw.json           │
│  • Slack provider                                                           │
│  • Heartbeat (30m, session: heartbeat, target: none)                        │
│  • Cron (MoltMarket notifications, etc.)                                    │
│  • Plugin: rmp_adapter (/root/.openclaw/plugins/rmp_adapter)                │
│  • auth.order: nvidia:default → nvidia:key2 → nvidia:key3                   │
└───────────────┬───────────────────────────────┬─────────────────────────────┘
                │ before_message_write          │ hooks/agent (internal)
                │ (sync block → RMP)            │
                ▼                               ▼
┌───────────────────────────┐     ┌───────────────────────────────────────────┐
│  RMP API (:8000)          │     │  OpenClaw Agent (gpt-5-nano / NVIDIA fb)  │
│  FastAPI                  │     │  • Tools, skills, workspace files           │
│  POST /tasks              │────▶│  • Per-task session: agent:main:rmp_task_*│
│  signals, memory, export  │     │  • JSONL poll for completion              │
└───────────────┬───────────┘     └───────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Temporal (:7233)  +  rmp-worker                                            │
│  • GenericTaskWorkflow — plan-driven steps + child workflows                │
│  • CatalogTaskWorkflow — 6 step templates                                   │
│  • Reconciler + janitor — stale/stuck/orphan repair                         │
└───────────────┬─────────────────────────────────────────────────────────────┘
                │
    ┌───────────┼───────────┬──────────────┬────────────────┐
    ▼           ▼           ▼              ▼                ▼
 Postgres    Qdrant     Artifacts      Side-effect       Safe Harbor
 (rmp_db)   (vectors)   (SHA-256 FS)   receipts          scanners/watchdog
              ▲
              │ embeddings (advisory; fail-soft if embedder down)
              └── Mem0 + local Qdrant

        ┌─────────────────────────────────────┐
        │  LLM orchestration (RMP)            │
        │  quota_broker + usage_monitor       │
        │  balanced rotation · max 2 slots    │
        │  /data/llm_quota.json               │
        │  /data/llm_usage.json               │
        └─────────────────────────────────────┘
```

### systemd services

| Unit | Purpose |
|------|---------|
| `openclaw-gateway` | Slack + agent gateway; env from `/etc/openclaw/openclaw.env`; **ExecStartPre:** `ops/sync_nvidia_keys.py` |
| `rmp-api` | FastAPI; `/etc/rmp/rmp.env` + `/etc/openclaw/openclaw.env`; key sync pre-start |
| `rmp-worker` | Temporal worker (`openclaw-tasks` queue); same env + key sync |
| `temporal` | Official Temporal server 1.30.1 on this host's Postgres (`temporal`, `temporal_visibility`), frontend `127.0.0.1:7233`. One node, not Temporal Cloud. |
| `rmp-canary.timer` | Hourly health check (`*:07` — staggered from heartbeat) |
| `rmp-memory-canary.timer` | Memory canary at 04:37, 10:37, 16:37, 22:37 CEST |
| `rmp-janitor.timer` | Daily workflow janitor (`ops/workflow_janitor.py`) |
| `rmp-backup.timer` | Daily Postgres backup |

---

## 3. Message flow (Slack → reply)

### 3.1 Normal Slack DM

1. Kirill sends a DM → OpenClaw Slack provider receives it (session key `agent:main:slack:channel:…`).
2. **`rmp_adapter`** claims the turn (`inbound_claim` / `message_received`) and `POST /tasks` with the **full Slack text** (not a truncated intent). **No native OpenClaw Slack fallback** — fail closed (claim + suppress) if intake/API is down.
3. **`before_message_write`**: blocks Slack DM persistence / native assistant turns while RMP owns delivery (`{ block: true }`).
4. **Intake Analyst** (not Aura) classifies the message against hybrid-retrieved evidence into one of four relation classes, then applies a decision (`clarify` / attach / wait / rebuild / guided / fresh). See §5.0.0.
5. If work proceeds, Temporal starts **GenericTaskWorkflow** or **CatalogTaskWorkflow** (catalog type is **intake-LLM only**).
6. Aura executes in `agent:main:rmp_task_*` via **`send_to_openclaw`** (`deliver: false`).
7. **Process Evaluator** (not Aura; session `rmp_verify_*`) must **accept** the result before Slack. Conversational replies are gated too. Canary/system stay on the short deterministic path.
8. **`notify_slack_user`** → idempotent RMP `chat.postMessage`. Insufficient work is reworked (attempts 1–19) or escalated with a diagnosis (attempt 20).

### 3.2 Cron (e.g. MoltMarket)

Same path via plugin (`tags: cron`). OpenClaw cron job uses `delivery.mode: none`; RMP owns Slack delivery when actionable.

### 3.3 Heartbeat

Every 30 minutes OpenClaw runs heartbeat on **isolated session** `heartbeat` (not the Slack DM session). The plugin **does not** create RMP tasks for internal heartbeat triggers (avoids workflow/API floods). When heartbeat text is routed, **`HEARTBEAT_OK`** completions are **not** delivered to Slack. LLM **reserve/release** hooks also skip `trigger === 'heartbeat'`.

### 3.4 Hourly canary

`rmp-canary.timer` → `ops/canary.sh` → `POST /tasks` with intent `RMP CANARY: Reply with exactly CANARY_OK`.  
Success/failure is logged internally; **no Slack notification** to Kirill (canary/system suppression).

---

## 4. OpenClaw layer

| Item | Location / value |
|------|------------------|
| Config | `/root/.openclaw/openclaw.json` |
| Workspace | `/root/.openclaw/workspace` (`USER.md`, `MEMORY.md`, `memory/*.md`, `HEARTBEAT.md`) |
| Agent sessions | `/root/.openclaw/agents/main/sessions/*.jsonl` |
| Primary model | `openai/gpt-5-nano` (`agentRuntime.id: "openclaw"`); fallbacks MiniMax M3 → DeepSeek V4 Flash; intake same chain |
| Model fallbacks | gpt-5-nano → MiniMax → DeepSeek (no GLM). HTTP 410 = skip to next model |
| Concurrency | OpenClaw `maxConcurrent: 2`; RMP `max_concurrent: 3`, `min_interval_sec: 5` |
| LLM keys | `/etc/openclaw/openclaw.env`: `OPENAI_API_KEY` (gpt-5-nano), `NVIDIA_API_KEY`, `NVIDIA_API_KEY_2`, optional `_3` |
| Auth profiles | Shared SQLite `authProfiles.store` in `state/openclaw.sqlite` (2026.9+); synced from env via `ops/sync_nvidia_keys.py`. Do not recreate leftover `auth-profiles.json` (triggers AUTH_PROFILE_MIGRATION_REQUIRED). |
| Auth rotation order | `auth.order.openai`: `openai:default`. `auth.order.nvidia`: `nvidia:default` → `nvidia:key2` → `nvidia:key3`. Never pin `nvidia:keyN` on `openai/*` sessions.
| Plugin | `/root/.openclaw/plugins/rmp_adapter/index.js` |
| Slack sockets | This host: exactly one `openclaw-gateway`. See [`docs/runbooks/slack-sockets.md`](docs/runbooks/slack-sockets.md). Never `apps.connections.open`. Other hosts undetectable. |
| Slack streaming | `{"mode":"off","nativeTransport":false}` (RMP owns Slack delivery; object form required by OpenClaw 2026.7+) |
| OpenClaw version | `2026.9.1` (requires Node ≥ 22.22.3) |
| Hook sessions | `/hooks/agent` uses `sessionMode: persistent` plus `hooks.allowedSessionKeyPrefixes: ["hook:", "agent:main:rmp_"]` (2026.9 defaults isolated and will not persist `rmp_*` keys otherwise) |
| Post-update patches | `ops/upgrade_openclaw.sh` (or `patch_openclaw.sh` then `ops/verify_openclaw_patch.sh`; see §4.1) |

OpenClaw also has built-in profile cooldown/rotation on 429; RMP’s quota broker **caps cooldowns**, **balances load across keys**, **limits concurrent agent runs**, and syncs `authProfiles.state` `usageStats.cooldownUntil`.

### Plugin hooks (`rmp_adapter`)

| Hook | Behavior |
|------|----------|
| `message_received` / `inbound_claim` | Slack DM → `POST /tasks`; claim turn so native OpenClaw never replies (fail closed) |
| `before_message_write` | Block Slack DM / assistant writes while RMP owns delivery; cron task create; skip internal heartbeat routing |
| `message_sending` | Suppress native Slack during active RMP user task; strip interim tool-planning text; cancel pure-ack messages |
| `before_agent_run` | **`POST /api/llm/reserve`** — balanced key + concurrency slot for gateway sessions (not `rmp_task_*`, `rmp_verify_*`, `rmp_intake_*`, Slack/main, heartbeat) |
| `agent_end` | **`POST /api/llm/release`** — free slot for same session exclusions |
| `llm_output` | **`POST /api/llm/record-gateway`** — token/request accounting for gateway LLM turns |
| Assistant ack block | Pure system acks not written to `agent:main:main` transcript |

On task create the plugin also **prefetches** process memory (`GET /memory/process/{id}/context`) and passes `initial_memory_block` into the workflow payload.

Tools exposed to the agent: `rmp_task_create`, `rmp_task_status`, `rmp_memory_recall` (optional; normal path is automatic routing).

### 4.0.1 Galaxy web capability stack

OpenClaw plugin **`aura_web`** (`/root/.openclaw/plugins/aura_web`) plus localhost FastAPI **web-stack** (`http://127.0.0.1:8791`, systemd `aura-web-backends`) give Aura multi-backend web tools:

| Class | Tools |
|-------|--------|
| Search | Brave `web_search` (default), `langsearch_search` (key in `plugins.entries.langsearch`) |
| Fetch | `jina_reader` (r.jina.ai), built-in `web_fetch`, `crawl4ai` |
| Crawl / extract | `crawl4ai`, `crawlee_crawl`, `scrapling`, `scrapegraph_extract` |
| Interact | OpenClaw `browser`, `browser_use`, `obscura_browse` |
| Status | `web_capability_status` |

RMP **`WebCapabilityAnalyzer`** (`app/orchestrator/web_capability.py`) classifies intake intents (`search|fetch|crawl|adaptive_extract|schema_extract|interact|none`), injects a **WEB CAPABILITY BRIEF** into execute prompts / `initial_memory_block`, and may *suggest* `browser_automation` as a soft note. **Catalog workflow assignment is intake-LLM only** (`catalog_hint` → policy). Keyword/regex catalog patterns are advisory (`soft_catalog_candidates` in the intake prompt) — they must not hard-route Slack DMs after intake. Agent-visible docs: workspace `TOOLS.md`.

Paid APIs (Perplexity, Firecrawl, Tavily) are **not** configured unless keys are added later.

### 4.1 OpenClaw dist patches (re-apply after every `npm install -g openclaw`)

Run: `bash /root/.openclaw/rmp/ops/upgrade_openclaw.sh` (`make upgrade-openclaw`). Never `openclaw onboard`, never `doctor --force`, never hand-edit dist.

| Patch | Why |
|-------|-----|
| Hook persistence | Always run `before_message_write` when `hookRunner` exists (typed `hasHooks` can miss the plugin) |
| Announce suppress | Skip native subagent announce for `rmp_(task\|verify\|intake)_` sessions |
| Minimal bootstrap | TOOLS.md only for those RMP sessions |
| Slack suppress | `deliverReplies` calls `__RMP_SUPPRESS_NATIVE_SLACK` so RMP owns delivery |
| allowUnsafe passthrough | OpenClaw 2026.7 dropped `allowUnsafeExternalContent` from HTTP `/hooks/agent` normalize; RMP needs it (or auto-enable for `rmp_*` keys) to avoid EXTERNAL wrap → `NO_REPLY` on JSON intake |
| LLM idle 5s | `DEFAULT_LLM_IDLE_TIMEOUT_MS` 120s → 5s so idle silence fails fast and NVIDIA keys rotate |
| Session canonical scan | Skip in-flight `{}` placeholders with `entry_valid != 1`; do not fail-closed the whole store on parseable pending rows (2026.9 `entry_valid` triggers otherwise poison every `/hooks/agent`) |
| Session timestamp drift | Ignore `session_nodes.updated_at` vs JSON `updatedAt` mismatch (often tens of ms); stock parser returns null and `/hooks/agent` throws `SESSION_CANONICAL_KEY_MIGRATION_REQUIRED` |
| HTTP 410 skip | Classify 410 as `model_not_found` (next fallback), not timeout/idle retry |
| Model fallbacks | **Left enabled** — gpt-5-nano → MiniMax → DeepSeek (do not re-apply legacy no-fallback disable; do not restore GLM) |

Upgrade checklist: `ops/upgrade_openclaw.sh` (backup → Node ≥ 22.22.3 → `npm install -g openclaw@latest` → `OPENCLAW_SERVICE_REPAIR_POLICY=external openclaw doctor --fix --non-interactive` → restore RMP config keys / `TOOLS.md` → `ops/settle_openclaw_sessions.py` (never drop `session_nodes` entry_valid triggers) → `patch_openclaw.sh` → verify → skills → restart if no user tasks → `make production-check`). Do not run `openclaw update` (it re-runs doctor/restart on its own).

---

## 5. RMP (Reliability and Memory Plane) layer — orchestration, workflows & process control

RMP is the **control plane** for Aura. OpenClaw provides LLM + tools + Slack transport; RMP provides **durability**, **process semantics**, **memory injection**, **completion gates**, **reconciliation**, and **LLM resource orchestration**. Every user-visible turn becomes a Postgres-backed **Task** driven by a Temporal **workflow**; OpenClaw runs inside isolated **child workflows** / activities as the execution engine.

### 5.0.0 Analyst control plane (binding)

**Constitution:** [`docs/CONCEPT_TREE.md`](docs/CONCEPT_TREE.md). This section is the runtime summary; if it drifts, the constitution wins.

Two **non-Aura** RMP agents own judgment. Retrieval is **evidence only** — never assignment. Regex catalog patterns and vector similarity are advisory.

| Role | Session | Job |
|------|---------|-----|
| **Intake Analyst** | `agent:main:rmp_intake_*` | Classify each user message against running work, finished work, global memory, or “this is new.” If unsure, **clarify** via RMP Slack. |
| **Process Evaluator** | `agent:main:rmp_verify_*` | Aura never reaches Slack first. Dual gate: deterministic evidence, then semantic judge. Fail closed on parse/tool errors. |
| **Aura** | `agent:main:rmp_task_*` | Execution engine only (tools, code, search, replies). |

**Four relation classes (intake):**

1. Related to a **currently running** process → `attach_active`, `wait_active`, or `rebuild_stale`
2. Related to **finished** work → `create_guided` (citations of past task + memory)
3. Related only to **global/user memory** → guided/conversational with memory brief
4. **New** → `create_fresh` (new Task row for this message — **not** conversational amnesia)

**Decisions:** `clarify`, `attach_active`, `wait_active`, `rebuild_stale`, `create_guided`, `create_fresh`, `skip_valid`, `skip_noop`, `supersede`, `spawn_process`. `skip_*` still persist an intake decision and a short user-visible ack — messages are never silently dropped. Low confidence **must not** default to `create_fresh` (clarify, or wait if a single clear **user** active target exists). Health canary/heartbeat/system tasks are not user work: never `wait_active` / `attach_active` onto them. If intake LLM/workflow fails, user DMs still **`create_fresh` a Task and start `GenericTaskWorkflow`**. `execution_mode=conversational` only selects the one-step RMP plan (still `rmp_task_*` + RMP `chat.postMessage`); it is **not** a native Slack reply and must never skip the plugin → `/tasks` → intake → Temporal path.

**Always-gated Slack:** including greetings. Canary/system tasks stay on the short deterministic path (no 20-turn loops, no user clarify).

**Session continuity:** `create_fresh` still starts a new Task/workflow, but `POST /tasks` injects **RECENT DIALOGUE** from prior same-`session_key` `task_messages` (user + assistant Slack turns). User-local clock is injected as a fact (`USER LOCAL TIME`), not a greeting-phrase policy. Vector recall is advisory (OpenAI `text-embedding-3-small`); dialogue injection must not depend on embeddings.

**Attempt law:** attempts 1–9 rework with evaluator critique; attempt 10 `strategy_change`; 11–19 continue; attempt 20 stop and Slack a diagnosis of what was tried and why it was insufficient.

**Hybrid retrieval:** Postgres FTS + Qdrant dense + user memory + active-task metadata, fused with Reciprocal Rank Fusion (k=60). Intake Analyst adjudicates.

### 5.0.1 Universal Task Intake (Phase 4–5 + analyst)

Before `POST /tasks` starts Aura execution, intake runs when `task_registry.enabled`:

1. **Layer 1 — Fast path** — idempotency; duplicate same-session intent → `attach_active`; active recurrence → `wait_active`; health-canary LLM bypass; deterministic `skip_valid` / `skip_noop` / `supersede` for recurrent jobs (still logged + ack)
2. **Layer 2 — Hybrid evidence pack** — FTS + dense + memory + active metadata (RRF). Vector similarity is **advisory**; it must not auto-attach or auto-create.
3. **Layer 3 — Intake Analyst** — first one typed Jev request (`app/decisions/intake.py`, `jev.intake_mode`), used only above its thresholds; otherwise the LLM turn `classify_task_intake` on `rmp_intake_*`. The API fallback also asks Jev before `create_fresh` at confidence 0.
4. **Policy engine** (`intake_decision_engine.py`) — validates catalog ids, low-confidence clarify, modes: `off` | `shadow` | `enforce`

**Plugin:** all Slack DMs route through `POST /tasks`; stop commands still signal directly.

**Durable tasks:** `task_kind=durable` + `spawn_leg`; `spawn_process` API; legs linked via `ProcessRun.parent_process_run_id`.

Supplementary user/cron text is stored in `task_messages`. Terminal tasks indexed into `task_registry_entries` + Qdrant (including evaluator summary).

**Completion rework:** global attempt counter on the process run; strategy change at 10; user diagnosis at 20. Canaries exempt.

### 5.0 Control-plane overview

```
Slack / cron
     │
     ▼
rmp_adapter (sync block → POST /tasks)
     │
     ▼
RMP API ── Intake Analyst (hybrid evidence; decisions including clarify)
     │
     ▼
Task + ProcessRun + Event rows
     │
     ▼
Temporal workflow (GenericTaskWorkflow | CatalogTaskWorkflow)
     │
     ├── generate_process_plan
     ├── for each plan step:
     │      GenericExecuteChildWorkflow | CatalogStepChildWorkflow
     │           ├── PROCESS BRIEF + build_process_memory_context
     │           ├── reserve_profile → send_to_openclaw → release_profile
     │           ├── extract_agent_facts + evaluate_step_predicate
     │           └── decide_step_outcome (orchestrator)
     ├── Process Evaluator (always-gated: evidence then semantic judge)
     ├── notify_slack_user only after accept (or attempt-20 diagnosis)
     └── execute_compensation on terminal failure
```

**Enhancement model:** OpenClaw alone = stateless agent turns in JSONL. With RMP modules, each turn gains:

| Capability | Without RMP | With RMP |
|------------|-------------|----------|
| Durability | Session JSONL only | Postgres ledger + Temporal replay |
| Multi-step work | Single agent loop | Plan-driven steps with child workflows |
| Completion | LLM self-reports | Predicate gates + evidence + quality review |
| Memory | Workspace MEMORY.md | Process-scoped inject + vector recall + promotion |
| Slack delivery | Gateway streaming | Idempotent, suppressed during tasks, policy-filtered |
| LLM keys | OpenClaw rotation only | Balanced 3-key pool, concurrency cap, usage ledger |
| Failure recovery | Manual | Reconciler, janitor, compensation, retry signals |

### 5.1 Task lifecycle & routing

**Creation (`POST /tasks`):**

1. Plugin or API submits `intent`, `session_key`, optional `tags`, `task_type`, `idempotency_key`.
2. API deduplicates by idempotency key; returns existing task if terminal and caller retries.
3. Creates `Task` (status `pending` → `running`), `ProcessRun`, initial `Event`.
4. Starts Temporal workflow on queue `openclaw-tasks` with payload including `initial_memory_block` when prefetched.

**Signals (`POST /tasks/{id}/signal`):**

| Signal | Effect |
|--------|--------|
| `user_input` | Resume blocked step / inject user reply |
| `cancel` | User stop; workflow sets `stopped_by_user` |
| `approve` | Human gate for sensitive catalog steps |
| `retry` | Re-run failed/compensated task |

**Leases:** Worker activities acquire short-lived leases on ProcessRun to prevent duplicate side effects during Temporal retries.

**Idempotency:** Slack receipts stored in `SideEffectReceipt`; duplicate `notify_slack_user` calls are no-ops.

### 5.2 Workflow types

#### GenericTaskWorkflow (`app/workflows/generic_task.py`)

Primary path for Slack DMs, cron, and most automation.

1. **`initialize_process_run`** — binds task to ProcessRun, sets status `running`.
2. **`_plan_driven_loop`** — program-owned step machine (legacy monolithic loop removed):
   - **`generate_process_plan`** — single LLM call produces JSON plan (`steps[]` with `name`, `prompt`, `predicate_id`).
   - **`save_process_plan`** — persists `plan_json` on ProcessRun.
   - **`build_process_memory_context`** — once at start; refreshed after each completed step (skipped vector for canaries via `skip_vector`).
   - For each plan step, up to **3 attempts** via **`GenericExecuteChildWorkflow`** child.
   - On step `blocked` → task `pending_user_input`; on exhausted failures → **`execute_compensation`**.
3. **`decide_completion_gate`** — evidence check, then Process Evaluator (quality LLM) for user work. Quality LLM is skipped only for internal/canary/heartbeat (`is_internal_task`).
4. **`notify_slack_user`** — final delivery unless internal/canary/heartbeat suppression applies.

Exception path: any uncaught workflow error triggers compensation + user-facing error message (formatted via `notification_policy`).

#### CatalogTaskWorkflow (`app/workflows/catalog_task.py`)

Structured multi-step flows for repeatable business processes. Seven templates in `app/workflows/catalog.py`:

| # | Template | Typical predicates |
|---|----------|-------------------|
| 1 | Account registration | form fill, confirmation |
| 2 | Login / sign-in | auth success |
| 3 | Email verification | inbox / code |
| 4 | Procurement | cart, checkout |
| 5 | Outreach / email | draft, send |
| 6 | Browser automation | navigation, extract |
| 7 | Tool self-upgrade | draft → tests → approval → controlled restart → verify |

Each catalog step maps to a **`predicate_id`** and runs in **`CatalogStepChildWorkflow`** (same OpenClaw dispatch pattern as generic children).

#### Child workflows

| Workflow | Role |
|----------|------|
| `GenericExecuteChildWorkflow` | One OpenClaw dispatch per generic plan step; isolated session `agent:main:rmp_task_{id}` |
| `CatalogStepChildWorkflow` | One dispatch per catalog step with template-specific prompt |

Child IDs use pattern `{task_id}-plan-{step_name}-{attempt}` for orphan detection by reconciler/janitor.

### 5.3 Process management (Postgres entities)

| Entity | Purpose |
|--------|---------|
| **Task** | User-visible unit: intent, status, session_key, tags, next_check |
| **ProcessRun** | Execution instance: `plan_json`, lease holder, terminal reason |
| **Step** | Named plan step: status (`pending`/`running`/`completed`/`failed`/`blocked`/`compensated`), attempt count |
| **Observation** | Agent output snapshot per step attempt |
| **Event** | Audit trail (created, status changes, signals) |
| **MemoryItem** | Process-scoped and promoted memories |
| **SideEffectReceipt** | Dedup keys for Slack posts |
| **Artifact** | SHA-256 content-addressed outputs |

**Status machine (simplified):**

```
pending → running → completed
                 ├→ failed → (retry) → running
                 ├→ pending_user_input → (user_input) → running
                 ├→ stopped_by_user
                 └→ compensated (lease released, steps annotated)
```

**Compensation (`execute_compensation`):** Releases lease, marks open steps `compensated`, writes process memory annotation, sets terminal task/process state — without overwriting with a generic `failed` when already compensated.

### 5.4 Orchestrator modules (`app/orchestrator/`)

The orchestrator keeps **program logic** in charge; the LLM proposes actions and facts, but **does not** unilaterally declare step completion.

| Module | File | Role |
|--------|------|------|
| **Step predicates** | `step_predicates.py` | `extract_agent_facts()` parses fenced JSON `{ "facts": {...} }`; `evaluate_step_predicate(predicate_id, ...)` returns pass/fail/issues |
| **Decision engine** | `decision_engine.py` | `decide_step_outcome()` — maps predicate result + attempt budget to `completed`/`pending`/`failed`/`blocked`; `decide_completion_gate()` — evidence + quality LLM (user work always evaluated) |
| **Prompt policy** | `prompt_policy.py` | Forbids workspace `memory_search` during RMP steps. Regex `GENERIC_PROFILES` exist as leftover tables; `resolve_generic_profile` returns `None` so they do not assign tool budgets on user DMs |

**Predicate examples:** `generic_deliver`, read/summarize/recall-specific gates, catalog-specific gates. Legacy `task_status` JSON in agent output is still parsed as fallback but **predicates are authoritative**.

### 5.5 OpenClaw execution path (`openclaw_activities.py`)

**`send_to_openclaw`** is the bridge from Temporal to OpenClaw:

1. **`wait_for_dispatch_sync()`** — quota broker pacing (per-key interval, cooldown-aware key pick).
2. **`reserve_profile(session_key)`** — atomic concurrency slot + balanced key assignment; pins `authProfileOverride` on session.
3. **`POST /hooks/agent`** — dispatches prompt to isolated RMP session; `deliver: false` (RMP owns Slack); `allowUnsafeExternalContent: true` so OpenClaw does not EXTERNAL-wrap trusted RMP prompts (otherwise structured JSON intake returns `NO_REPLY`).
4. **JSONL poll** — waits for terminal assistant message; scans up to 3 recent session files if session ID rotated.
5. **`release_profile()`** — always in `finally`.
6. On 429: `record_rate_limit`, rotate, retry (up to 12× for chat).

**Gateway path (non-RMP sessions):** Plugin hooks call **`POST /api/llm/reserve`** at `before_agent_run` and **`POST /api/llm/release`** at `agent_end` — same broker pool, so gateway cron/misc sessions share the concurrency cap with worker dispatches.

**Session bootstrap:** OpenClaw patch loads minimal TOOLS.md for `rmp_task_*` / `rmp_verify_*` / `rmp_intake_*` sessions — no MEMORY.md pollution.

**Session profile assign:** `assign_openclaw_session_profile` only updates keys that already have a `sessionId` (call after `/hooks/agent` creates the session). Pre-creating keys races OpenClaw 2026.7+ `CronSessionLifecycleClaimError`.

### 5.6 Reconciliation, janitor & background control

| Component | File / unit | Behavior |
|-----------|-------------|----------|
| **Reconciler** | `app/reconciler.py` | Cron activity: stale tasks (>20m), stuck RUNNING workflows (>45m terminate + repair), orphan `*-plan-*` child cleanup; skips internal/canary Slack nudges |
| **Workflow janitor** | `ops/workflow_janitor.py`, `rmp-janitor.timer` | Daily sweep of orphaned Temporal executions >24h |
| **Canary timers** | `rmp-canary.timer`, `rmp-memory-canary.timer` | Hourly E2E + 6h memory/vector canary; results in logs + `data/last_memory_canary.json` |
| **Readiness** | `GET /api/production/readiness` | Stuck workflow count, canary freshness, LLM orchestration snapshot |

This layer makes the single-VPS deployment **set-and-forget**: transient worker crashes, hung OpenClaw sessions, and orphaned children are repaired without operator intervention.

### 5.7 API (`app/api/server.py`)

Key endpoints:

| Endpoint | Purpose |
|----------|---------|
| `POST /tasks` | Create task + start Temporal workflow |
| `GET /tasks/{id}` | Status |
| `POST /tasks/{id}/signal` | `user_input`, `cancel`, `approve`, `retry` |
| `POST /tasks/{id}/cancel` | Cancel workflow |
| `GET /api/production/readiness` | Go-live readiness score |
| `GET /metrics` | Prometheus counters |
| `GET /tasks/{id}/export` | Postmortem bundle |
| `/memory/*` | Write, lookup, compact, graph, process context |
| `GET /memory/vector/status` | Qdrant / embedder health |
| `POST /api/notify-user` | RMP-owned Slack notice without Aura (intake unavailable / idle stop) |
| `POST /api/llm/reserve` | Acquire concurrency slot + balanced NVIDIA profile |
| `POST /api/llm/release` | Release slot for session |
| `GET /api/llm/orchestration` | Active slots, in-flight per key, rotation mode |
| `GET /api/llm/usage` | Daily per-key requests/tokens by source |
| `POST /api/llm/record-gateway` | Record gateway LLM token usage (plugin hook) |

Auth: `X-RMP-API-Key` (settings + `/etc/rmp/rmp.env`).

### 5.8 Activities (selected)

| Activity | File | Role |
|----------|------|------|
| `generate_process_plan` | `plan_activities.py` | One-shot LLM plan; conversational fast-path for trivial intents |
| `send_to_openclaw` | `openclaw_activities.py` | Reserve → quota gate → dispatch → JSONL poll → release |
| `build_process_memory_context` | memory activities | Ordered Postgres + vector merge for inject block |
| `notify_slack_user` | `openclaw_activities.py` | Idempotent Slack delivery |
| `verify_response_quality` | `openclaw_activities.py` | LLM quality review (extra NVIDIA call, same broker) |
| `parse_agent_evaluation` | `openclaw_activities.py` | Facts-first via `extract_agent_facts` |
| `execute_compensation` | compensation activities | Terminal failure cleanup |
| DB activities | `db_activities.py` | ProcessRun, Step, Observation, Event, leases |
| Side effects | `side_effects.py` | Slack receipt dedup |

### 5.9 Notification policy (`app/notification_policy.py`)

Central rules for **what reaches Slack**:

- Suppress canary, heartbeat, system-tagged tasks (success and failure)
- Strip leading `CANARY_OK` / `HEARTBEAT_OK` from user-facing text
- Suppress native OpenClaw Slack during active RMP user tasks (plugin `message_sending`)
- Format workflow errors (timeout, NVIDIA rate limit, key rotation) instead of generic “Activity task failed”

### 5.10 LLM orchestration — quota broker & usage monitor

All RMP-issued NVIDIA calls (worker dispatch, embeddings, quality review) and gateway agent runs share one orchestration layer.

#### Quota broker (`app/llm/quota_broker.py`)

| Mechanism | Behavior |
|-----------|----------|
| **Keys** | `NVIDIA_API_KEY` → `nvidia:default`, `NVIDIA_API_KEY_2` → `nvidia:key2`, `NVIDIA_API_KEY_3` → `nvidia:key3` |
| **Rotation mode** | `balanced` — pick key with lowest daily load score: `requests + tokens/5000 + in_flight×50` |
| **Per-key pacing** | `min_interval_sec: 5` (live settings); no global gap across all keys beyond broker pacing |
| **Concurrency** | `max_concurrent: 3` — `reserve_profile()` / `release_profile()` track `active_slots`, `session_slots`, per-key `in_flight` |
| **On 429** | Escalating cooldown per key (15s → 30s → 60s → 120s); rotate; retry up to 6× (embed) or 12× (chat) |
| **Session pinning** | `assign_openclaw_session_profile()` sets `authProfileOverride` on the SQLite auth store (do not recreate leftover `auth-profiles.json`) |
| **State** | `/root/.openclaw/rmp/data/llm_quota.json` |
| **OpenClaw sync** | Syncs SQLite `authProfiles` `usageStats.cooldownUntil` to match broker |
| **Config** | `settings.json` → `llm_quota.*` |

```json
"llm_quota": {
  "provider": "nvidia",
  "min_interval_sec": 5.0,
  "max_wait_sec": 1800.0,
  "cooldown_steps_sec": [15, 30, 60, 120],
  "rotation_mode": "balanced",
  "max_concurrent": 3
}
```

**Key sync:** `ops/sync_nvidia_keys.py` writes env keys into SQLite `authProfiles.store` on service start. Manual: `make sync-nvidia-keys`. Live probe: `ops/nvidia_key_probe.py`. Do not recreate leftover `auth-profiles.json`.

#### Usage monitor (`app/llm/usage_monitor.py`)

Tracks **requests + tokens** per key per day, by source:

| Source | Origin |
|--------|--------|
| `openclaw_llm` | Worker `send_to_openclaw` dispatches |
| `openclaw_hook` | Gateway sessions recorded via plugin `llm_output` |
| `embed` | `nvidia_embed.py` vector calls |
| `rate_limit_429` | Cooldown events |
| `probe` | `nvidia_key_probe.py` |

- **State:** `/root/.openclaw/rmp/data/llm_usage.json`
- **Scrape:** Also reads OpenClaw session JSONL for gateway turns not captured by hooks
- **Report:** `ops/llm_usage_report.py`, `GET /api/llm/usage`; healthcheck prints daily per-key summary
- **Honesty:** `nvidia:unknown` is treated as unset when a model id is present (`openai/` → `openai:default`, `nvidia/` → `nvidia:default`). Only `rolling_24h` events with a model are rewritten. Day-bucket `nvidia:unknown` totals stay; summary annotates those UTC days. Never invent `nvidia:keyN`.

**Design choice:** Chat and embeddings share one key pool — avoids total quota overrun; bulk vector seeding may pace Slack turns slightly. Balanced rotation **aims for equal load** but instant parity is not guaranteed under burst traffic.

---

## 6. Data & memory

### 6.1 Postgres (`rmp_db`)

Core entities: `Task`, `ProcessRun`, `Step`, `Observation`, `Event`, `MemoryItem`, `Artifact`, `SideEffectReceipt`, `memory_links`.

### 6.2 Vector memory (Mem0 + Qdrant)

| Setting | Value |
|---------|-------|
| Qdrant mode | **server** (Docker on `127.0.0.1:6333`) |
| Qdrant data | `/root/.openclaw/rmp/data/qdrant-server` (bind mount) |
| Collection | `rmp_memories_openai_3small` |
| Embedder | OpenAI `text-embedding-3-small` (1536-d). NVIDIA NIM embedders remain HTTP 410 EOL. See live `settings.json` `vector_memory.*` |
| Config | `settings.json` → `vector_memory.*` |
| Implementation | `app/memory/vector.py` (`embed_query_text`) |

**Why server mode:** Embedded Qdrant uses a single-process `.lock` file. With both `rmp-api` and `rmp-worker` using vector memory, only one process could hold the lock — the other reported `ready: false`. A shared Qdrant server lets both connect over HTTP.

**Ops:**

```bash
# Start/stop (also systemd unit rmp-qdrant.service)
make -C /root/.openclaw/rmp qdrant
bash /root/.openclaw/rmp/ops/stop_qdrant.sh

# One-time migration from embedded storage
make -C /root/.openclaw/rmp migrate-qdrant
```

Compose file: `docker-compose.qdrant.yml`. `rmp-api` and `rmp-worker` systemd units `After=rmp-qdrant.service`.

**Key resolution:** Chat NVIDIA keys from `/etc/openclaw/openclaw.env` → SQLite `authProfiles.store`. Embeddings use `OPENAI_API_KEY` → `openai:default`.

**Rate limits:** NVIDIA chat/embed helpers still go through `wait_for_dispatch_sync()`. OpenAI embeddings use the OpenAI embeddings API directly.

**Chat vs embed “compatibility”:** Chat models never see raw vectors. Embeddings only rank text chunks for retrieval. Conversational continuity must not depend on embeddings. NVIDIA NIM embedders probed 2026-09-05 return HTTP 410 EOL; live replacement is OpenAI `text-embedding-3-small`.

**Semantic recall:** Memory lookup with `query=` merges Postgres rows and Qdrant vector hits (`MemoryRouter.read`).

**Seeding:** `app/memory/seed.py` — chunks workspace markdown + re-indexes Postgres memories:

```bash
make -C /root/.openclaw/rmp seed-vector-memory
```

Wipe Qdrant before changing embedder model or dimensions. Bulk seed is paced (~15–20 min for ~45 chunks) due to shared quota broker.

**Index (2026-06-04):** 45 vectors (31 workspace + 14 Postgres).

**Future option:** Local embeddings (Ollama / sentence-transformers) — no API quota, ops tradeoff; not required for chat-model compatibility.

### 6.3 Memory promotion (`app/memory/promotion.py`)

Episodic → semantic/procedural pipeline (partial vs original 4-stage vision).

### 6.4 Artifacts

Content-addressed store under `/root/.openclaw/rmp/data/artifacts`; completion outputs registered per task.

### 6.5 OpenClaw workspace memory

Daily notes: `/root/.openclaw/workspace/memory/YYYY-MM-DD.md`  
Long-term notes: `MEMORY.md`, `USER.md`

Workspace files are **human notes** for main/heartbeat. They are **not** the production recall path for RMP-owned Slack DMs (process-scoped Postgres + RECENT DIALOGUE). Qdrant is advisory dense retrieval (OpenAI `text-embedding-3-small`). Conversational continuity must not depend on embeddings.

---

## 7. Safe Harbor (`/root/aura_safe_harbor`)

Operational scripts outside RMP:

- **Moltbook scanners** — `moltbook_*_scan.js`, swarm runners, rate-limit tracking
- **Kairos core** — `restore_powers.js` (cron every 5m), `task_watchdog.js`, `self_auditor.js`
- **Stubs / partial** — `deep_core.js` (calls RMP compact), `auditor.js`, `memory_chunker.js`

**Retired (2026-06):** Agent Cerebro (`agent-cerebro` pip package, `cerebro_data/`) — superseded by RMP memory (§6).

Scanner catalog synced by RMP (`app/scanners/`) when `development_mode: false`.

---

## 8. Observability & ops

| Mechanism | Status |
|-----------|--------|
| Readiness API | ✅ ~93% at go-live |
| Prometheus `/metrics` | ✅ |
| Hourly canary | ✅ `:07` past each hour |
| Daily backup | ✅ `ops/backup.sh` |
| Runbooks | ✅ `docs/runbooks/` |
| OTLP trace backend | ✅ Phoenix + OTel collector (`make observability`); `telemetry.otlp_endpoint` set |
| Production Temporal | One official server on Postgres. Not a cluster and not Temporal Cloud. |
| Alerting webhook | ⏳ Config present; disabled by default |

### Operator commands

```bash
cd /root/.openclaw/rmp
make production-check       # health + OpenClaw patch verify
make canary                 # manual E2E canary
make readiness              # full readiness JSON
make backup
make sync-nvidia-keys       # env → SQLite authProfiles.store
make seed-vector-memory     # re-index workspace + Postgres → Qdrant
make rollback               # return to dev quiet mode (ops/rollback_dev.sh)

python3 ops/llm_usage_report.py      # daily per-key usage by source
python3 ops/nvidia_key_probe.py      # live NVIDIA smoke test on all keys

systemctl status temporal rmp-api rmp-worker openclaw-gateway
systemctl status rmp-memory-canary.timer rmp-janitor.timer rmp-canary.timer
journalctl -u rmp-worker -f
journalctl -u openclaw-gateway -f
tail -f /tmp/rmp_plugin_debug.log
curl -s http://127.0.0.1:8000/memory/vector/status | jq
curl -s -H "X-RMP-API-Key: $RMP_API_KEY" http://127.0.0.1:8000/api/llm/orchestration | jq
curl -s -H "X-RMP-API-Key: $RMP_API_KEY" http://127.0.0.1:8000/api/llm/usage | jq
cat /root/.openclaw/rmp/data/llm_quota.json   # key cooldown + slot state
cat /root/.openclaw/rmp/data/llm_usage.json   # daily usage ledger
```

---

## 9. What is complete vs. partial (Phase 2 honest status)

**Phase 1 overstated behavioral dominance.** Phase 2–4 close the gaps below. Full orchestration detail is in **§5**; this section is a completion checklist. See `VISION_COMPLETION_PROGRESS.md` §Phase 2 for step-by-step evidence.

### Layer A — Control plane ✅ (Phase 2)

- **`execute_compensation`** — releases lease, marks open steps `compensated`, writes process memory annotation, sets terminal task/process state  
- **Generic/catalog exception paths** — compensation without overwriting with `failed`  
- **Reconciler** — syncs ProcessRun on repair; skips internal tasks before Slack notify  
- **Child workflow hygiene** — lease release + step finalization; dead `_dispatch_step` removed  
- **`GenericExecuteChildWorkflow`** — one isolated OpenClaw dispatch per plan step  

### Layer B — Non-LLM orchestration ✅ (Phase 2)

- **Persisted `plan_json`** on ProcessRun; **`generate_process_plan`** (one LLM call at start)  
- **`step_predicates.py`** — facts JSON + deterministic predicate gates (not LLM `task_status`)  
- **Plan-driven generic loop** — program owns step advance/retry/fail (legacy `user_evaluation_loop` removed Phase 3)  
- **Catalog steps** map to `predicate_id`; finish path uses **`decide_completion_gate`** (user work always hits Process Evaluator)  
- **Intent profiles** — leftover tables only; `resolve_generic_profile` is disabled (no regex assignment)  

### Layer C — Process memory daily use ✅ (Phase 2)

- **Minimal bootstrap** for `rmp_task_*` / `rmp_verify_*` sessions (TOOLS.md only — no MEMORY.md) via OpenClaw patch  
- **Unified inject** — all dispatch uses `build_process_memory_context`; child workflows receive prebuilt block  
- **Memory-first executor prompts** — forbid workspace `memory_search` during RMP steps  
- **Empty-process fallback** — user pinned/semantic from `read_ordered` when process pool empty  
- **Plugin** — no `[AUTO-ROUTED]` pollution; no `process_type_hint` (intake LLM assigns); prefetch `/memory/process/{id}/context`; **`rmp_memory_recall`** tool  
- **Vector `pinned` index** on write; promotion dedup by content prefix  

### Verification ✅ (Phase 2)

- **Full pytest suite** (zero exclusions)  
- **`make production-check`** — health + readiness including stuck-workflow and memory-canary checks  
- **`ops/canary_slack_memory.sh`** — live canary; writes `data/last_memory_canary.json`  

### Phase 3 — Production hardening ✅ (set-and-forget on single VPS)

- **Vector search timeout** (20s) — postgres-only fallback via `MemoryRouter`  
- **Memory built once per plan loop** — refresh only after completed steps; `skip_vector` for canaries  
- **Reconciler active repair** — terminate stuck RUNNING workflows after 45m; orphan `*-plan-*` child cleanup  
- **Daily workflow janitor** — `ops/workflow_janitor.py` + `rmp-janitor.timer`  
- **`STALE_TASK_MINUTES` = 20** — internal/canary tasks skip Slack nudge spam  
- **Scheduled memory canary** — `rmp-memory-canary.timer` (6h); readiness checks `last_memory_canary.json` age  
- **OpenClaw poll fallback** — scan last 3 session JSONL files on session-id rotation  
- **API prefetch** — `initial_memory_block` in workflow payload at task create  
- **`parse_agent_evaluation`** — facts-first via `extract_agent_facts`  

### Phase 4 — LLM orchestration ✅

- **Balanced key rotation** — `rotation_mode: balanced` across 3 NVIDIA accounts  
- **Concurrency cap** — `max_concurrent: 3` with `reserve_profile` / `release_profile`  
- **Per-key pacing** — `min_interval_sec: 5`  
- **Usage monitor** — per-key daily requests/tokens by source (`llm_usage.json`)  
- **Plugin LLM hooks** — `before_agent_run`, `agent_end`, `llm_output` on gateway sessions  
- **Session profile pinning** — `authProfileOverride` on RMP dispatches  
- **Heartbeat isolation** — no RMP task creation or LLM reserve on internal heartbeat triggers  

### Implemented well (foundation, pre–Phase 2) ✅

- External RMP sidecar with Temporal ledger  
- Plugin fail-closed routing (sync HTTP)  
- Evidence-based completion + idempotency + reconciler  
- Seven workflow catalog templates  
- Vector memory infrastructure + graph API  
- **RMP keys module** — `app/llm/model_policy.py` + SQLite auth sync; gpt-5-nano primary, NVIDIA MiniMax/DeepSeek fallbacks  
- **Multi-key NVIDIA rotation** + RMP quota broker (chat + embeddings) + balanced load + usage ledger  
- Backups, canary, readiness, go-live/rollback  
- Slack noise suppression (canary/heartbeat/ack stripping)  

### Still partial vs. original vision ⚠️

| Area | Gap |
|------|-----|
| **OTLP live backend** | Phoenix UI at `http://127.0.0.1:6006`; OTLP HTTP `4318`; RMP exports to `http://127.0.0.1:4318/v1/traces` |
| **Production Temporal** | Official server on this host's Postgres. Single frontend. Not Cloud and not a second node. |
| **Safe harbor integration** | `deep_core` compact wired; `auditor.js` / `memory_chunker.js` exist under `/root/aura_safe_harbor` (deeper consolidation optional) |
| **72h soak** | Recommended post-go-live; not formally signed off (Phase 7) |
| **NVIDIA free-tier quota** | Mitigated by 3-key balanced rotation + concurrency cap + usage monitor; org-level TPM may still cap throughput |
| **Live Slack memory canary** | Scheduled via `rmp-memory-canary.timer`; result file at `data/last_memory_canary.json` |
| **Stuck Temporal workflows** | Reconciler terminates at 45m; daily janitor for orphans >24h |

**Phase 6 completed:** spawn leg lineage (`parent_process_run_id`), skip_noop/supersede fast paths, MoltMarket skill symlink, intake canaries including attach/wait and supersede.

---

## 10. Recommended next steps

Prioritized for stability first, then capability.

### P0 — Stability (do first)

1. **Confirm timers** — `systemctl status rmp-memory-canary.timer rmp-janitor.timer rmp-canary.timer`.  
2. **Complete 72h soak** — monitor `make readiness`, canary timers, `llm_quota.json`, stuck-workflow count.  
3. **Verify Slack path** — DMs; single reply, Postgres task rows, process memory context API hits.

### P1 — Observability & ops

4. **OTLP stack** — done on this host (`make observability` + `telemetry.otlp_endpoint`).  
5. **Enable alerting webhook** — internal channel for canary failures.  
6. **Upgrade Temporal** — Temporal Cloud or self-hosted HA cluster.

### P2 — Product depth

7. **Catalog workflow usage** — test registration/login templates against real targets.  
8. **Embedding eval** — optional hit@3 benchmark on real Slack questions.

### P3 — Architecture completion

9. **Safe harbor consolidation** — optionally deepen wiring of `auditor.js` / `memory_chunker.js` into RMP (implementations already present).  
10. **MoltMarket skill** — fix missing `SKILL.md` path so cron stops erroring.

---

## 11. Key file index

```
/root/.openclaw/
├── openclaw.json                 # Gateway, model, auth.order, heartbeat, Slack
├── openclaw.env → /etc/openclaw/openclaw.env
│                                 # NVIDIA_API_KEY, NVIDIA_API_KEY_2, NVIDIA_API_KEY_3
├── agents/main/agent/
│   └── (auth: SQLite authProfiles.store — do not recreate auth-profiles.json)
├── workspace/                    # Agent memory & bootstrap files
├── plugins/rmp_adapter/          # Slack → RMP routing + LLM reserve/release hooks
├── agents/main/sessions/         # JSONL transcripts
└── rmp/
    ├── ARCHITECTURE.md           # ← this file
    ├── DEVELOPMENT_PLAN.md
    ├── PRODUCTION_PLAN.md
    ├── settings.json             # vector_memory, llm_quota, production, …
    ├── data/
    │   ├── llm_quota.json        # Per-key cooldowns, slots, in_flight
    │   ├── llm_usage.json        # Daily per-key usage by source
    │   ├── last_memory_canary.json
    │   └── qdrant/               # Vector index
    ├── app/
    │   ├── orchestrator/         # decision_engine, step_predicates, prompt_policy
    │   ├── workflows/            # generic_task, catalog_task, child workflows
    │   ├── reconciler.py         # Stale task + stuck workflow repair
    │   ├── llm/
    │   │   ├── quota_broker.py   # NVIDIA key gate, balanced rotation, slots
    │   │   └── usage_monitor.py  # Per-key daily usage ledger
    │   └── memory/
    │       ├── vector.py         # Mem0/Qdrant service
    │       ├── nvidia_embed.py   # NVIDIA embeddings + broker
    │       ├── mistral_embed.py  # Legacy adapter (unused in prod config)
    │       └── seed.py           # Workspace + Postgres re-index
    ├── ops/
    │   ├── sync_nvidia_keys.py   # Env → SQLite auth (NVIDIA + optional OpenAI)
    │   ├── nvidia_key_probe.py   # Live NVIDIA smoke test
    │   ├── llm_usage_report.py   # Usage summary CLI
    │   ├── workflow_janitor.py   # Orphan Temporal cleanup
    │   ├── canary.sh, backup.sh, …
    └── tests/                    # pytest (orchestration, usage, workflows, …)

/root/aura_safe_harbor/           # Scanners, watchdog, legacy Kairos
/etc/rmp/rmp.env                  # RMP_API_KEY, DATABASE_URL
/etc/openclaw/openclaw.env        # OPENAI_API_KEY, NVIDIA_API_KEY, NVIDIA_API_KEY_2, NVIDIA_API_KEY_3
/etc/systemd/system/              # Service units (ExecStartPre key sync)
```

---

## 12. Secrets & config (reference)

| Secret | Location | Used for |
|--------|----------|----------|
| RMP API key | `/etc/rmp/rmp.env`, `settings.json` | RMP API auth |
| **NVIDIA API keys** | `/etc/openclaw/openclaw.env` → SQLite `authProfiles.store` | NVIDIA fallbacks (MiniMax/DeepSeek) + embeddings (advisory) |
| Slack bot/app tokens | `openclaw.json` | Slack gateway |
| Postgres | `DATABASE_URL` in `/etc/rmp/rmp.env` | RMP ledger |
| Mistral API key | `/etc/openclaw/openclaw.env` (optional) | **Legacy** — not used by current RMP config |
| **OpenAI API key** | `/etc/openclaw/openclaw.env` `OPENAI_API_KEY` → SQLite `openai:default` | gpt-5-nano primary. Never git, Slack, or `openclaw.json`. Do not set `models.providers.openai.apiKey` to `${OPENAI_API_KEY}` — that marks the provider cold if the gateway process env is stale. Missing key: readiness **warn** `openai_key_missing`; OpenClaw falls back to MiniMax. |

Do not commit secrets to git.

---

*For day-to-day status, run `make production-check` and inspect the dashboard at `http://127.0.0.1:8000/` (localhost only).*

## Jev decisions

TypeSafe Jev (`jev-1.13.0`) answers typed questions in two places, each with its
own `off` / `shadow` / `enforce` mode under `settings.json` `jev`:

- **Intake** — one request per DM (relation, target task, action, execution mode,
  catalog, web intent). An answer above its thresholds becomes the same result the
  intake LLM returns, so `apply_intake_policy` still decides; anything else goes to
  the LLM turn. Shadow stores the proposal in `task_intake_decisions.llm_raw.jev`.
- **Memory promotion** — support, durability and user-scope checks before a fact
  becomes semantic or pinned user memory.

Jev calls use no OpenClaw session and no LLM quota slot. See
[the Jev runbook](docs/runbooks/jev.md) for evaluation, rollout and rollback.
