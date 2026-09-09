# Concept-tree constitution progress log

Append-only. Each plan step adds a dated entry below. Do not rewrite prior entries.

---

## 2026-09-05 — Step 1: CONCEPT_TREE.md source of truth

- **Status:** complete
- **What landed:** `/root/.openclaw/rmp/docs/CONCEPT_TREE.md` — purpose, conflict law, concept tree, MUST/MUST NOT/SHOULD, turn paths, memory, orchestration, models, anti-patterns, contradiction register, pointer map, residual product risks. No TBD/TODO/placeholder sections.
- **Sources used:** `request.txt`, `request_2`, ARCHITECTURE.md, CONTROL_PLANE_PROGRESS.md, Cursor rules, USER.md/TOOLS.md, parent transcripts 68658cb8 / 3ad82511.
- **Verification:** `rg TBD|TODO|placeholder|coming soon` on CONCEPT_TREE.md → no matches.
- **Remaining:** Steps 2–7 (rules, docs, session identity, classifiers, embeddings, production close).

---

## 2026-09-05 — Step 2: Cursor rules from SoT

- **Status:** complete
- **What landed:** Thin always-on `/root/.cursor/rules/rmp-architecture.mdc` (gpt-5-nano, fail-closed, no static patches, pointer to CONCEPT_TREE.md). Scoped `rmp-intake-catalog.mdc` and `rmp-memory-dialogue.mdc`. Nested `/root/.openclaw/rmp/.cursor/rules/rmp-architecture.mdc` is byte-identical (MiniMax-as-primary copy gone). `openclaw-upgrade.mdc` already matched SoT models.
- **Verification:** `cmp` identical on both architecture copies; no MiniMax-as-primary / GLM-as-chat-fallback in the binding.
- **Remaining:** Steps 3–7.

---

## 2026-09-05 — Step 3: Documentation coherence

- **Status:** complete
- **What landed:** ARCHITECTURE.md points at CONCEPT_TREE.md; `before_agent_run` not `before_agent_start`; SQLite auth store (no live `auth-profiles.json` as the store); vector wording is fail-soft/EOL not “working nv-embed-v1”. TOOLS.md / AGENTS.md heartbeat-cron stock playbook replaced with isolated-heartbeat / RMP-owns-Slack note.
- **Verification:** `rg` ARCHITECTURE + both `rmp-architecture.mdc`: primary `openai/gpt-5-nano`; no `before_agent_start`. Workspace no longer tells Aura to deliver cron natively or reach out after 8h as policy.
- **Remaining:** Steps 4–7.

---

## 2026-09-05 — Step 4: Session identity integrity

- **Status:** complete
- **What landed:** User DMs persist the real Slack conversation key (`agent:main:slack:channel:d…`), not `agent:main:main`. Plugin `pickSlackSessionKey` prefers inbound Slack keys then SQLite `session_nodes` (sessions.json is gone). `POST /tasks` rewrites user+main via the same discover. RECENT DIALOGUE and active-task lookup include main as backfill for Slack keys. Intake attach treats Slack→legacy-main as the same conversation; canary main does not attach onto Slack user tasks. Dual-written plugin copies.
- **Verification:** `./venv/bin/pytest -q tests/test_session_identity.py tests/test_session_dialogue.py tests/test_task_intake_extended.py tests/test_intake_handlers.py` — 40 passed. `cmp` plugin copies identical; `node --check` OK. Active user tasks at land: 0.
- **Remaining:** Steps 5–7 (classifiers, embeddings, production close + idle restart).

---

## 2026-09-05 — Step 5: Static classifiers off the live assignment path

- **Status:** complete
- **What landed:** Catalog templates stay; `catalog_assignment_from_intake` assigns only when intake ran (degraded/off → GenericTaskWorkflow). `GENERIC_PROFILES` regex no longer selects tool budgets on user DMs. WEB CAPABILITY BRIEF injects only for real web classes (`search|fetch|crawl|…`); chat/awareness/RMP-opinion get none; execute prompts do not re-derive a brief from regex. Plugin + Temporal stop only on a whole-message `stop|abort|cancel|halt` (optional `please` / punctuation). Dual-written plugin.
- **Verification:** 59 passed (`test_stop_command`, `test_web_capability`, `test_catalog`, `test_process_brief`, `test_vision_completion`, …). Awareness / “RMP and tools” / “are you here” do not open `tool_self_upgrade` or inject crawl-tool briefs.
- **Remaining:** Steps 6–7.

---

## 2026-09-05 — Step 6: Memory path honest and finished

- **Status:** complete (not-ready branch)
- **What landed:** Probed NVIDIA embeddings with the live key (status codes only; key not logged). `nvidia/llama-nemotron-embed-1b-v2` and `nvidia/nv-embed-v1` both HTTP **410 EOL** (gone 2026-08-25). Vector memory is disabled/`ready=false` with `not_ready_reason`. Health and readiness report the attempted model and must not claim ready. Registry upsert/search skip embeds while disabled so we do not hammer 410. Conversational path remains RECENT DIALOGUE + process memory. No re-ingest (no live embedder).
- **Verification:** 11 passed (`test_vector_memory`, `test_task_registry_vector_store`, `test_memory_reliability`).
- **Remaining:** Step 7 production close.

---

## 2026-09-05 — Step 7: Evaluator/canary integrity + production close

- **Status:** complete
- **What landed:** User DMs run Process Evaluator (`skip_quality = is_internal_task(...)`). Canaries stay out of `user_visible_active_tasks` / wait_active. `CANARY_OK` remains a silent ack (never user Slack). Task-registry dense probe warns instead of blocking when the embedder is not-ready. Idle restart of rmp-api / rmp-worker / openclaw-gateway.
- **Verification:** `./venv/bin/pytest -q` — **324 passed**. `/health` `vector_memory.ready=false` with model `nvidia/llama-nemotron-embed-1b-v2`. `make production-check` — readiness **18 pass / 4 warn / 0 fail**; intake execution_mode conversational; intake latency 7s; OpenClaw patch verify OK; primary `openai/gpt-5-nano`.
- **Remaining product risks (not unfinished constitution):** dual Slack sockets; hourly canary sharing OpenClaw lanes with user intake; usage ledger `nvidia:unknown`; single-node Temporal. NVIDIA NIM embeddings stay 410.

---
