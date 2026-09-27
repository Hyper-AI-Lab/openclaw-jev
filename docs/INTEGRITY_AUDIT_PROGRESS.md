# Integrity match-audit progress log

Append-only. Each plan step adds a dated entry below. Do not rewrite prior entries. Separate from `CONCEPT_TREE_PROGRESS.md` (constitution close is done).

---

## 2026-09-05 — Step 1: Constitution match report

- **Status:** complete
- **What landed:** `/root/.openclaw/rmp/docs/CONSTITUTION_MATCH.md` — original plan 1–7 vs live; founding pillars vs live Slack path; later stones as SUPERSEDED; live MISMATCH / doc-only drift / PRODUCT-RISK inventory.
- **Live Slack path:** MATCH (claim → `/tasks` → intake → Temporal → evaluator → RMP Slack). Evaluator on for user work including conversational. Catalog assign only from intake. Nested rules and dual plugins identical. Vector honestly not-ready.
- **MISMATCH to fix next:** (1) `POST /tasks` timeout + recovery fail is claim+suppress silence — no RMP error DM; (2) whole-message stop with no active task is silent; (3) stale ARCHITECTURE/README/TOOLS/AGENTS claims (quality skip, `process_type_hint`, intent profiles, pytest count, MiniMax/GLM primary, MEMORY.md as production recall).
- **Not missing vision:** native/parallel OpenClaw replies, Aura-first clarify, keyword assignment, MiniMax-as-primary, live `nv-embed-v1`.
- **Verification:** classified inventory with file evidence; `rg TBD|TODO|placeholder|NotImplemented` on live `app/` + `rmp_adapter` Slack path — no unfinished delivery stubs.
- **Remaining:** Steps 2–7 (SoT repairs, embedder, usage, canary lanes, socket detection, production close).

---

## 2026-09-05 — Step 2: Repair SoT mismatches

- **Status:** complete
- **What landed:** `POST /api/notify-user` + `app/notify_user.py` sends an RMP-owned Slack notice (`chat.postMessage`, idempotent) when `POST /tasks` timeout recovery fails (`intake_unavailable`) or a whole-message stop has no active task (`stop_idle`). Plugin still `{ handled: true }` (fail closed, never native). Dual-written `rmp_adapter`. Stale ARCHITECTURE/README/TOOLS/AGENTS/MEMORY.md claims corrected (quality skip, `process_type_hint`, intent profiles, pytest count, MiniMax/GLM primary, MEMORY.md as production recall).
- **Verification:** `./venv/bin/pytest -q tests/test_notify_user.py tests/test_process_evaluator.py tests/test_stop_command.py tests/test_notification_policy.py` — 25 passed. `cmp` plugin copies identical; `node --check` OK.
- **Remaining:** Steps 3–7.

---

## 2026-09-05 — Step 3: Embedder probe and switch

- **Status:** complete (OpenAI live)
- **Probe (status/dims only, keys not logged):** OpenAI `text-embedding-3-small` HTTP 200 dims 1536; `text-embedding-3-large` HTTP 200 dims 3072. NVIDIA `llama-nemotron-embed-1b-v2` and `nv-embed-v1` still HTTP 410.
- **What landed:** Live embedder `openai` / `text-embedding-3-small` / 1536-d. New collections `rmp_memories_openai_3small` and `rmp_task_registry_openai_3small`. `ready=true` only after a successful embed probe (`embed_query_text`). Seed: workspace 45/45, Postgres 2435/2435. Registry re-embed: 2717/2717. `/health` `vector_memory.ready=true`.
- **Verification:** `./venv/bin/pytest -q tests/test_vector_memory.py tests/test_vector_gate.py tests/test_task_registry_vector_store.py tests/test_memory_reliability.py tests/test_readiness.py` — 22 passed (later 21 with slack_sockets tests). Live `/health` ready with model `text-embedding-3-small`.
- **Remaining:** Steps 4–7.

---

## 2026-09-05 — Step 4: Usage ledger honesty

- **Status:** complete
- **What landed:** `resolve_usage_profile_id` attributes OpenAI/`gpt-*`/`text-embedding-*` to `openai:default`, NVIDIA models to `nvidia:default` or the session pin, and never buckets known OpenAI turns as `nvidia:unknown`. JSONL scraper records non-NVIDIA assistant turns. Session pin lookup uses SQLite `session_nodes` (not leftover sessions.json). Gateway `/api/llm/record-gateway` uses the same resolver.
- **Verification:** `./venv/bin/pytest -q tests/test_usage_monitor.py` — 6 passed (gpt-5-nano → openai:default; MiniMax → nvidia:default).
- **Remaining:** Steps 5–7.

---

## 2026-09-05 — Step 5: Canary must not starve user intake

- **Status:** complete
- **What landed:** User `rmp_intake_*` / `rmp_task_*` / `rmp_verify_*` reserves preempt one canary/heartbeat slot when `max_concurrent` is full (`_preempt_canary_slot`). Short stale TTL (6 min) now also covers `rmp_intake_*` and heartbeat keys. User slots are not preempted. Idle unchanged. Users are still not `wait_active` onto canaries.
- **Verification:** `./venv/bin/pytest -q tests/test_quota_broker.py` — 9 passed (user intake preempts canary; does not preempt another user).
- **Remaining:** Steps 6–7.

---

## 2026-09-05 — Step 6: Dual Slack socket detection (this host)

- **Status:** complete
- **What landed:** Readiness check `slack_sockets` counts `openclaw-gateway` PIDs via systemd MainPID + `/proc/*/comm`. Pass if exactly one; warn if zero or more than one. Does not call `apps.connections.open` (that would open another socket). Other-host sockets remain undetectable from this VPS.
- **Verification:** `./venv/bin/pytest -q tests/test_readiness.py` — slack_sockets in `run_all_checks`; single=pass, dual=warn. This host: one gateway PID.
- **Remaining:** Step 7 production close.

---

## 2026-09-05 — Step 7: Production close

- **Status:** complete
- **What landed:** Full pytest, `make production-check`, idle restart of rmp-api / rmp-worker / openclaw-gateway (0 active user tasks). Plugin copies identical. `/health` vector ready (`text-embedding-3-small`). Readiness **21 pass / 2 warn / 0 fail** (telemetry OTLP unset; memory canary last status=timeout).
- **Verification:** `./venv/bin/pytest -q` — **341 passed**. Production-check: patch verify OK, primary `openai/gpt-5-nano`, intake conversational, latency 8s. After restart: gateway live, `slack_sockets` pass (single gateway), `vector_memory` pass, `task_registry_vector` pass.
- **Remaining product risks (not unfinished audit items):** Temporal single-node `temporal-dev`; Galaxy/Safe Harbor (Obscura fail-soft); Slack sockets on **other hosts**; canary can still occupy a lane until a user reserve preempts it; historical `nvidia:unknown` rows in today's usage file (new OpenAI turns are attributed correctly); memory canary last run timed out. This pass does not mean the whole Aura roadmap is finished.

---

## 2026-09-05 — Follow-up: stop vs canary + web-brief false positive

- **Status:** complete
- **Trigger:** Late SoT-audit leftovers that were still live after Step 7. Registry `--reembed` finished 2717/2717, 0 errors (confirms Step 3; the killed `--all` rebuild was not needed).
- **What landed:** Whole-message `stop` now looks up `/active_user_task` (user workflows only). `/active_user_task` excludes `canary` as well as `cron`/`heartbeat`. Heartbeat/cron skip still uses `/active_task` (any running work). Dropped `what is the current` from `_SEARCH_RE` so status/chat questions do not get a WEB CAPABILITY BRIEF. Dual-written plugin.
- **Verification:** `./venv/bin/pytest -q tests/test_web_capability.py tests/test_notify_user.py` — 19 passed. `cmp` plugin copies identical. Idle restart via `ops/controlled_capability_restart.sh --gateway --rmp-if-idle` (0 active user tasks).
- **Still product-risk (not this follow-up):** `message_received` still does not return `{ handled: true }` (mitigated by `inbound_claim` / `before_dispatch`).

---

## 2026-09-09 — Pointer: product-risk close

Named leftover product/ops risks from the integrity close were executed in [`docs/PRODUCT_RISK_PROGRESS.md`](PRODUCT_RISK_PROGRESS.md) (fail-closed `message_received`, single-node Temporal WAL, quota pools, memory-canary honesty, Galaxy fail-soft, usage unknown-as-unset, this-host sockets runbook). This file is not rewritten. The Aura roadmap is not finished.

---

## 2026-09-27 — Pointer: vision integrity close

The founding-path close is in [`docs/VISION_CLOSE_PROGRESS.md`](VISION_CLOSE_PROGRESS.md). This file is not rewritten.



