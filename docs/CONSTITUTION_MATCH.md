# Constitution match audit

**Date:** 2026-09-05  
**Scope:** Original concept-tree plan (`concept_tree_constitution_e7a112e8`) + founding `request.txt` / `request_2` (after later Kirill stones) vs live Slack path.  
**Classes:** MATCH | MISMATCH | SUPERSEDED | PRODUCT-RISK  
**Live path:** plugin claim → `POST /tasks` → intake → Temporal → evaluator → RMP `chat.postMessage`.

Evidence from live code plus [Audit SoT vs live path](9d363fde-4b3d-4075-88f9-f36a13f462f8) and [Audit residual product risks](2cce8d05-38f3-4d7f-9c03-c8a4648bd703). Dual plugins and nested Cursor rules compared with `cmp`.

---

## A. Original constitution plan (steps 1–7) vs live

| Plan step | Class | Live evidence |
|-----------|-------|----------------|
| 1 SoT `CONCEPT_TREE.md` complete, no TBD | **MATCH** | `/root/.openclaw/rmp/docs/CONCEPT_TREE.md`; `rg TBD\|TODO\|placeholder` on that file → no unfinished sections |
| 2 Thin Cursor binding; nested copy identical | **MATCH** | `/root/.cursor/rules/rmp-architecture.mdc` = `/root/.openclaw/rmp/.cursor/rules/rmp-architecture.mdc` (md5 `4e6bcb24…`) |
| 3 ARCHITECTURE points at SoT; no MiniMax-primary / live `auth-profiles.json` as store | **MATCH** live config / **MISMATCH** leftover ARCHITECTURE/README claims below | `openclaw.json` primary `openai/gpt-6-luna`; no live `auth-profiles.json` |
| 4 Slack session key on user DMs | **MATCH** | Plugin `pickSlackSessionKey` `plugins/rmp_adapter/index.js`; API `persist_user_session_key` `app/task_registry/session_identity.py` |
| 5 Classifiers advisory; catalog only from intake | **MATCH** | `catalog_assignment_from_intake` `app/api/server.py` + `app/workflows/catalog.py`; `resolve_generic_profile` → `None` `app/orchestrator/prompt_policy.py` |
| 6 Embedder probe; honest not-ready if 410 | **MATCH** | `settings.json` `enabled: false`, `ready` false, model `nvidia/llama-nemotron-embed-1b-v2`, reason HTTP 410. Conversational path = RECENT DIALOGUE + process memory |
| 7 Evaluator on user DMs; canaries not user actives; pytest + production-check | **MATCH** | `skip_quality = is_internal_task(...)` `app/workflows/generic_task.py`; CONCEPT_TREE_PROGRESS Step 7: 324 pytest |

---

## B. Founding pillars vs live Slack path

| Pillar | Class | Evidence |
|--------|-------|----------|
| Process-scoped memory, not one soup | **MATCH** | `app/memory/router.py` PROCESS-SCOPED MEMORY; execute inject via `build_process_memory_context` |
| Program owns control plane; LLM executes | **MATCH** | Temporal `GenericTaskWorkflow` / `CatalogTaskWorkflow`; `step_predicates` + `decide_completion_gate` |
| OpenClaw is a tool; control plane survives upgrades | **MATCH** | `rmp_adapter` → `POST /tasks`; Aura `rmp_task_*` `deliver: false`; dist via `patch_openclaw.sh` |
| Every DM is a task or follow-up; never silently drop | **MISMATCH** (edge) | Happy path + `skip_*` persist + RMP ack (`intake_handlers.py`). After `POST /tasks` timeout **and** recovery fail: claim+suppress **silence** (`index.js` `routeSlackDmToRmp` / `inbound_claim` catch). Whole-message `stop` with **no** active task: claimed, no `/tasks`, no ack (`index.js` `isStopCommand` branch) |
| Non-Aura Intake Analyst; unsure → clarify | **MATCH** | `rmp_intake_*`; `intake_decision_engine.py` `low_confidence_clarify` / `low_confidence_wait` |
| Hybrid retrieval is evidence, not assignment | **MATCH** | `hybrid_retriever.py`; `vector_similarity_gate` always `None`; catalog from intake `catalog_hint` only |
| Evaluator before Slack; ~10 strategy / ~20 diagnosis | **MATCH** | `rmp_verify_*`; `completion_rework.py`; conversational still runs `verify_response_quality` then `notify_slack_user` |
| Aura never posts first; RMP delivers | **MATCH** | `{ handled: true }` even on error; `notify_slack_user` → `chat.postMessage` |
| Full user text to Aura when work proceeds | **MATCH** | Plugin posts full Slack text (no 500-char truncate) |
| Intake failure still RMP (never native) | **MATCH** | Deterministic `create_fresh` + Generic (`intake_activities.py`); plugin fail-closed |

---

## C. Later stones (encoded, not missing vision)

| Old wording | Class | Winning live claim |
|-------------|-------|-------------------|
| OpenClaw replies in parallel with task create | **SUPERSEDED** | Aura executes; RMP judges and delivers; plugin claims first |
| Always send DM to Aura before classification | **SUPERSEDED** | Clarify is RMP question, no Aura yet (`CONCEPT_TREE.md` §5.1) |
| Conversational = Slack-first / skip evaluator | **SUPERSEDED** | One-step **RMP** plan; still Temporal + evaluator (`plan_activities.py`, `generic_task.py`) |
| Native fallback if `POST /tasks` fails | **SUPERSEDED** | Fail closed. Preferred optional RMP-owned error DM is **not implemented** (see MISMATCH) |
| Keywords / `GENERIC_PROFILES` assign catalogs | **SUPERSEDED** | Intake LLM assigns; plugin sends no `process_type_hint` |
| `create_fresh` = empty mind | **SUPERSEDED** | New Task row + RECENT DIALOGUE |
| Ask user at attempt 10 | **SUPERSEDED** | Strategy change at 10; diagnosis at 20 |
| MiniMax/GLM as primary; no OpenAI | **SUPERSEDED** | `openai/gpt-6-luna` → `nvidia/openai/gpt-oss-20b`; no GLM, no DeepSeek, no MiniMax |
| `nv-embed-v1` working | **SUPERSEDED** | Honest vector not-ready (HTTP 410) |
| `auth-profiles.json` is the store | **SUPERSEDED** | SQLite `authProfiles.store`; live JSON file absent |

---

## D. Live Slack path detail

| Item | Class | Evidence |
|------|-------|----------|
| Dual plugin copies identical | **MATCH** | `/root/.openclaw/rmp/plugins/rmp_adapter/index.js` = `/root/.openclaw/plugins/rmp_adapter/index.js` |
| Plugin does not send `process_type_hint` | **MATCH** | `index.js` `createRmpTaskFromInbound` |
| Fail-closed `{ handled: true }` | **MATCH** | `inbound_claim` / `before_dispatch` catch still claims |
| User DMs persist `agent:main:slack:channel:d…` | **MATCH** | `pickSlackSessionKey` + `persist_user_session_key` |
| Conversational gated | **MATCH** | `tests/test_process_evaluator.py`; `generic_task.py` |
| `skip_quality` = `is_internal_task` only | **MATCH** (live) | `generic_task.py`; catalog never skips |
| Intake LLM/workflow fail → Generic + RMP Slack | **MATCH** | `intake_activities.py`; `execution_mode.py` |
| Primary `openai/gpt-6-luna`; idle ~5s; no GLM | **MATCH** | `openclaw.json`; dist `DEFAULT_LLM_IDLE_TIMEOUT_MS = 5e3` |
| Vector `/health` not claiming ready | **MATCH** | `server.py` + `memory/router.py` `ready: false` |
| Whole-message stop only | **MATCH** | Plugin `isStopCommand` + Temporal `is_whole_message_stop` |
| User DMs do not `wait_active` onto canaries | **MATCH** | `intake_decision_engine.py` `user_visible_active_tasks` |
| No `TODO`/`NotImplemented` on Slack path in `app/` or `rmp_adapter` | **MATCH** | Search clean; plugin `placeholder: '<>'` is API-key config |
| `POST /tasks` timeout + recovery fail → silence | **MISMATCH** | `index.js` `routeSlackDmToRmp` recover then throw; catch claims, no `notify_slack_user` |
| Stop with no active task → silence | **MISMATCH** | `index.js` stop branch returns true without `/tasks` or ack |
| ARCHITECTURE quality LLM “may skip when strong” | **MISMATCH** (doc-only) | `ARCHITECTURE.md` vs live `skip_quality = is_internal_task` |
| ARCHITECTURE plugin still sends `process_type_hint` | **MISMATCH** (doc-only) | `ARCHITECTURE.md` Layer C |
| ARCHITECTURE intent profiles as live assignment | **MISMATCH** (doc-only) | `ARCHITECTURE.md` §5.4 / Layer B vs `resolve_generic_profile` → `None` |
| ARCHITECTURE “106 pytest”; workspace MEMORY as authoritative recall | **MISMATCH** (doc-only) | `ARCHITECTURE.md` vs 324 pytest / process-scoped inject |
| README model claims | **MATCH** | `README.md` names `openai/gpt-6-luna` with the `nvidia/openai/gpt-oss-20b` fallback, as live `openclaw.json` does |
| AGENTS.md MAIN SESSION must read `MEMORY.md`; TOOLS.md `tasks.md` ledgers | **MISMATCH** (executor leftover) | Workspace vs constitution §6 |
| Dead `catalog_type_for_workflow` regex helper | **SUPERSEDED** | `catalog.py` — not called on live `/tasks` |
| API still *accepts* `process_type_hint` as advisory | **SUPERSEDED** | `server.py` TaskRequest; plugin does not send it |
| `evidence_high_confidence` unused on live skip | **SUPERSEDED** | `evidence.py`; callers are tests/history |

---

## E. Product / ops risks (not missing constitution)

| Item | Class | Evidence |
|------|-------|----------|
| Usage ledger `nvidia:unknown` + JSONL skips non-nvidia | **PRODUCT-RISK** | `app/llm/usage_monitor.py` `record_request`, `record_openclaw_jsonl_message` |
| Hourly canary can occupy an OpenClaw lane | **PRODUCT-RISK** | `quota_broker.py` short TTL; `rmp_intake_*` not in short-TTL list; shared `max_concurrent` |
| Dual Slack sockets (this host / other hosts) | **PRODUCT-RISK** | Readiness checks token + gateway `/health` only (`readiness.py`); this host: one `openclaw-gateway` at audit time |
| Temporal single-node `temporal-dev` | **PRODUCT-RISK** | Constitution §12 |
| NVIDIA hosted embeddings stay 410 | **PRODUCT-RISK** | This audit Step 3 will probe OpenAI embeddings |
| Galaxy Obscura unit failed (fail-soft) | **PRODUCT-RISK** | Not on Slack path |
| `message_received` backup does not return `{ handled: true }` | **PRODUCT-RISK** | Safe only because `inbound_claim` / `before_dispatch` claim first |
| Soft keyword hits shown to intake LLM | **PRODUCT-RISK** | Advisory by design; must not become assignment |
| WEB CAPABILITY BRIEF regex for real web classes | **PRODUCT-RISK** | Chat/`none` get empty brief; constitution forbids chat/opinion dumps |

---

## F. Integrity close list (this pass)

Fix as **MISMATCH** on the Slack path (Step 2):

1. RMP-owned error DM after `POST /tasks` timeout + recovery fail (never native).
2. Whole-message stop with no active task: short RMP ack (never silent drop).
3. Stale ARCHITECTURE / README / TOOLS / AGENTS claims listed above.

Then Steps 3–6 reduce named PRODUCT-RISKs (embedder, usage, canary lanes, socket detection) without violating §4. Step 7 production-close.

Do not treat SUPERSEDED founding wording as missing vision. Do not claim the whole Aura roadmap is finished.
