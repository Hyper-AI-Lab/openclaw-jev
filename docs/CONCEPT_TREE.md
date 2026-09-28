# Aura / RMP concept tree (source of truth)

**Status:** Binding constitution for this host.  
**Last updated:** 2026-09-28  
**How to use:** This document is *why* and *what must remain true*. [`ARCHITECTURE.md`](../ARCHITECTURE.md) is *how it is built*. Cursor rules are *must not violate while coding*. Aura-facing [`TOOLS.md`](/root/.openclaw/workspace/TOOLS.md) is executor notes, not this constitution.

If a later chat, plan, or nested rule disagrees with this file, this file wins after applying the conflict law in §2.

---

## 1. Purpose

Aura is Kirill’s Slack assistant on one Linux VPS. **OpenClaw is a tool/runtime** (Slack socket, LLM, tools, JSONL). **RMP (Reliability and Memory Plane) is the control plane**: it intercepts every user DM, records tasks and processes, injects the right memory, runs Temporal workflows, judges Aura’s work, and delivers Slack.

Founding ask ([`/root/request/request.txt`](/root/request/request.txt)):

- Memory is **process-scoped**, not one global soup.
- Task and process management is **programmatic and factual**, not “the LLM decides the control plane.”
- OpenClaw can be upgraded; RMP patches or talks to it as a tool so the control plane survives.

Founding control-plane ask ([`/root/request/request_2`](/root/request/request_2)):

- A **non-Aura** analyst classifies each DM against running work, finished work, global memory, or new.
- If unsure, **clarify with Kirill**.
- Hybrid retrieval (lexical + dense + metadata) is **evidence for that analyst**, not assignment.
- Aura never posts to Slack first. A **non-Aura** evaluator accepts, reworks, changes strategy (~10), or explains failure to Kirill (~20).

Kirill’s later stone (2026-08-12 and 2026-09-05): there is **no** path where Aura gets a Slack DM or replies on Slack without RMP interception. A “short conversational reply” that **bypasses** that path is forbidden. Intake may still choose a short **RMP** plan (`execution_mode=conversational`): that is still a task, Temporal, evaluator, and RMP `chat.postMessage`.

---

## 2. Conflict law (how contradictions are resolved)

Apply in order:

1. **Kirill’s latest explicit instruction** wins. Encode his *meaning*, not later jargon. Examples of latest-wins:
   - No direct OpenClaw Slack; every DM through the analyst; Aura never posts first.
   - No static components for narrow cases; workflows are situational.
   - Primary chat model is `openai/gpt-5-nano` (this **supersedes** June 2026 “I won’t add OpenAI” and August MiniMax-as-primary).
   - `conversational` as a **one-step RMP plan** is allowed. A short **native or ungated** reply is not.
2. Founding pillars in `request.txt` and `request_2` still bind unless (1) replaced them.
3. This file and the thin always-on Cursor rule [`/root/.cursor/rules/rmp-architecture.mdc`](/root/.cursor/rules/rmp-architecture.mdc) are the live coding invariants.
4. [`ARCHITECTURE.md`](../ARCHITECTURE.md) describes **runtime**. If it conflicts with (1)–(3), architecture is stale and must be corrected.
5. Historical plans, `docs/history/*`, `CODEBASE_INTELLIGENCE_REPORT.md`, and the nested copy [`/root/.openclaw/rmp/.cursor/rules/rmp-architecture.mdc`](/root/.openclaw/rmp/.cursor/rules/rmp-architecture.mdc) are **not** independent sources. Older surrogates (regex `GENERIC_PROFILES` as assignment, keyword catalog hard-route, one-shot native Slack fallback, MiniMax-as-primary, Kimi-as-primary, “no model fallback”) are **superseded**.

Phrases such as “fail closed” and “`create_fresh` ≠ amnesia” are **encodings** of Kirill’s words (interception; remember prior dialogue; “this is new” is a label, not a memory wipe). Keep both the encoding and the original meaning.

---

## 3. Concept tree — what the system is

```
Kirill (Slack DM, Japan / JST)
        │ Socket Mode
        ▼
OpenClaw gateway (:18789)          runtime only
  rmp_adapter claims + blocks native Slack
        │ POST /tasks (full text)
        ▼
RMP API (:8000) + Temporal worker
  Intake Analyst   session rmp_intake_*   (not Aura)
  Aura execute     session rmp_task_*     (tools / LLM)
  Process Evaluator session rmp_verify_*  (not Aura)
        │
        ▼
RMP chat.postMessage (idempotent)
```

### 3.1 Layers

| Layer | Path | Role |
|-------|------|------|
| Safe Harbor | `/root/aura_safe_harbor` | Legacy scanners/watchdog. Peripheral. Not the Slack path. |
| OpenClaw | `/root/.openclaw` | Slack socket, tools, JSONL, cron, heartbeat, plugins. Execution engine. |
| RMP | `/root/.openclaw/rmp` | FastAPI, Temporal, Postgres, Qdrant, quotas, Slack delivery, judgment. |
| Galaxy web | `aura-web-backends` `:8791`, Obscura `:9222` | Optional web tools for Aura when the **situation** needs them. |

### 3.2 Actors

| Actor | Session | May Slack? |
|-------|---------|------------|
| Kirill | Slack DM | Origin of user work |
| Intake Analyst | `agent:main:rmp_intake_*` | Clarify questions via RMP notify only |
| Aura | `agent:main:rmp_task_*` | Never first. Never native gateway delivery. |
| Process Evaluator | `agent:main:rmp_verify_*` | Diagnosis at attempt 20 via RMP |
| Heartbeat | off (`heartbeat.every: "0m"`); old `heartbeat` session archived | No |
| Health canary | tags `canary` / `system` | Silent on success; not user work |

### 3.3 Data stores

- **Postgres `rmp_db`:** Task, ProcessRun, Step, Observation, Event, MemoryItem, Artifact, SideEffectReceipt, `task_messages`, intake decisions, registry entries.
- **Qdrant:** advisory dense retrieval for memory/registry. Must not assign workflows. Health must not claim a dead embedder is ready.
- **OpenClaw JSONL:** executor transcripts for `rmp_*` sessions. Not the user-visible Slack log.
- **Workspace files:** `USER.md` (human facts, including Japan/JST), `TOOLS.md` (Aura-visible ops). Main-session `MEMORY.md` is **not** the production recall path for RMP-owned DMs.

### 3.4 Workflow kinds

- **GenericTaskWorkflow** — default for user DMs and most work. Plan-driven steps.
- **CatalogTaskWorkflow** — named templates (registration, login, email verify, procurement, outreach, browser automation, tool self-upgrade). **Assigned by Intake Analyst `catalog_hint`**, never by keyword alone.
- **IntakeWorkflow** — classify. If it fails, still create an RMP user task (`create_fresh` + Generic). Never native Slack.

---

## 4. Invariants

### MUST

- Every user Slack DM: plugin claim → `POST /tasks` → intake → Temporal → evaluator → RMP `chat.postMessage`.
- Every user message becomes a **task or a follow-up**. Never silently drop. `skip_*` still persist a decision and a short RMP ack.
- OpenClaw executes only inside `rmp_task_*` / `rmp_verify_*` / `rmp_intake_*` with `deliver: false`.
- Intake Analyst (not Aura) classifies relation: running / finished / memory / new. Uncertain → clarify.
- Retrieval (FTS, vectors, metadata) is **evidence**. Vector scores and regex catalog hits must not auto-attach or auto-create catalogs.
- After Aura acts, log it. Process Evaluator (not Aura) must **accept** before Slack, including greetings/chat, except the short deterministic canary/heartbeat/system path.
- Attempts: 1–9 rework; ~10 strategy change; 11–19 continue; ~20 stop and Slack a diagnosis.
- Process-scoped memory is injected on execute. Across `create_fresh`, prior same-conversation Slack turns are injected (RECENT DIALOGUE). “This is new” labels a **new task row**, not a new person.
- User-local clock is a **fact** (`USER LOCAL TIME` / Japan Standard Time). Server Europe/Berlin is not Kirill’s clock.
- Primary model: `openai/gpt-5-nano`. Fallback: `nvidia/openai/gpt-oss-20b` (NVIDIA-hosted; MiniMax M3 ended 2026-09-09). Subagent sessions, including the Process Evaluator, run on `openai/gpt-5-nano`. Intake: when `jev.intake_mode` is `enforce`, the pinned decision model `jev-1.13.0` answers typed intake questions first and counts only above program thresholds; otherwise the same chain decides. `apply_intake_policy` stays the authority either way.
- LLM idle ~5s then rotate keys/models. OpenAI alone gets 20s for the first byte; gaps between chunks stay 5s for every provider. HTTP 410 is skip (next model), not an idle retry.
- OpenAI key only in `/etc/openclaw/openclaw.env` → SQLite `openai:default`. Never `openclaw.json` interpolations, git, or Slack. Never pin `nvidia:keyN` on an `openai/*` session.
- After `npm install -g openclaw`, run `ops/upgrade_openclaw.sh`. Never `openclaw onboard`, never `doctor --force`, never hand-edit dist.
- Canaries are health checks: green → silent; failure → fix if possible else alert Kirill. They are not user work (`wait_active` / `attach_active` must ignore them). `CANARY_OK` must not appear in user Slack.
- One user-visible Slack reply per turn (no native + RMP double post).
- Secrets stay out of git and chat.

### MUST NOT

- Native OpenClaw Slack for user DMs, including “intake failed so let the gateway answer.”
- Plugin `process_type_hint` or keyword/`GENERIC_PROFILES`/web-regex **assignment** of catalogs or tool dumps.
- Static incident patches: greeting-word locks, “don’t list crawlers,” other content bans for one failure.
- Treat `execution_mode=conversational` as bypassing intake, evaluator, or RMP notify.
- Raise idle timeout to minutes “to be safe.”
- Wait/attach a user DM onto a canary/heartbeat/system task.
- Advertise models that are not in the live chain (e.g. Gemini, GLM as chat fallback).
- Recreate leftover `auth-profiles.json` beside the SQLite auth store.
- Depend on embeddings for conversational continuity.

### SHOULD

- Catalog templates exist as **libraries** of program-owned plans. The universe of work is larger than the catalog; GenericTaskWorkflow is the default.
- Aura may add tools via gated self-upgrade (draft → tests → approval → controlled restart → verify). Awareness questions are not upgrades.
- Evaluator may use bounded situational tools (health, readiness, web status) without becoming a second Aura.
- Nested Cursor rules under `rmp/.cursor/rules` stay **byte-identical** to `/root/.cursor/rules/rmp-architecture.mdc`.

---

## 5. Turn paths

### 5.1 User Slack DM

1. OpenClaw Slack provider receives the DM (`agent:main:slack:channel:…`).
2. `rmp_adapter` claims (`inbound_claim` / `before_dispatch` / `message_received`) and `POST /tasks` with **full text**. `{ handled: true }` even on error (no native reply).
3. `before_message_write` blocks native persistence/assistant turns while RMP owns delivery.
4. Intake: fast path (idempotency, duplicates, recurrence, canary bypass) → hybrid evidence pack → Intake Analyst (Jev typed decision above thresholds, else LLM) → policy (`enforce` in production).
5. Outcomes: clarify (RMP question, no Aura yet); attach/wait/rebuild on **user** actives; create_guided / create_fresh; skip with ack.
6. If work proceeds: Generic or Catalog Temporal workflow. Conversational → usually one deliver step (still RMP).
7. Aura in `rmp_task_*`. Program-owned plan and code predicates advance steps.
8. Process Evaluator in `rmp_verify_*` accepts or reworks.
9. RMP `chat.postMessage`, idempotent. Assistant text stored on `task_messages`.

**Stop:** A **whole-message** stop/cancel/abort/halt may signal the active Temporal workflow (programmatic control). Incidental “stop” inside a normal sentence must still go through intake.

### 5.2 Cron

Same RMP path with cron tags. OpenClaw cron `delivery.mode: none`. RMP owns Slack if anything is user-visible.

### 5.3 Heartbeat

**Off.** `apply_openclaw_policy` enforces `agents.defaults.heartbeat.every: "0m"`, so OpenClaw keeps its `heartbeat-main` monitor job disabled across upgrades. RMP canaries own liveness. The old `agent:main:heartbeat` session is archived with its transcript kept. It never used a tool in 1,200+ runs, yet it re-billed its whole growing context every 30 minutes, which was 89% of transcript tokens.

If a heartbeat ever runs again, the plugin still creates no RMP task for it and `HEARTBEAT_OK` is not delivered. Stock OpenClaw “reach out after 8h / deliver cron to a channel” text in workspace files is **not** live policy.

### 5.4 Health canary

`rmp-canary.timer` → `POST /tasks` with canary tags. Deterministic `CANARY_OK`. No Slack on success. Skip starting a canary when a **user** task is already active. Canaries share OpenClaw concurrency with user work — a residual product risk, not an excuse to `wait_active` the user onto the canary.

---

## 6. Memory doctrine

| Layer | Role |
|-------|------|
| Process-scoped Postgres memory | Primary recall for the current process run (episodic / working / pinned as implemented). |
| RECENT DIALOGUE (`task_messages`) | Same Slack **conversation** continuity across `create_fresh`. Must key off the real Slack session, not `agent:main:main`. |
| USER LOCAL TIME | Clock **fact** in the user timezone (Asia/Tokyo). Not a greeting-phrase table. |
| Hybrid retrieval | Evidence pack for Intake Analyst. Advisory. Fail-soft if vectors die. |
| Workspace MEMORY.md | Human notes for main/heartbeat. Forbidden as first recall during RMP execute (`PROCESS-SCOPED MEMORY` / dialogue instead). |

`create_fresh` means: new Task row for this message. Kirill still said the previous lines. Point out “this is new work” when it **is** new; do not wipe the dialogue.

---

## 7. Orchestration doctrine

- LLM may **draft a plan once**. The program stores it and advances steps.
- Step completion is **code predicates** on structured output, not Aura saying she is done.
- Compensation, leases, reconciler, janitor are program-owned.
- Evaluator is always on for user-visible work. Canaries use the short deterministic path (`max_rework=0`).
- Low confidence must not default to silent `create_fresh`. Clarify, or wait if exactly one **user** active target exists.

---

## 8. Models, keys, idle

| Item | Live law |
|------|----------|
| Primary | `openai/gpt-5-nano` (`agentRuntime.id: openclaw`) |
| Fallbacks | `nvidia/openai/gpt-oss-20b`: NVIDIA auth with key rotation, despite the `openai/` model id. No GLM, DeepSeek or MiniMax (all HTTP 410). No Gemini unless configured. |
| Subagents / Process Evaluator | `openai/gpt-5-nano` (`agents.defaults.subagents.model`). |
| Idle | ~5s then rotate. Do not restore 120s. First byte: OpenAI 20s (`RMP_OPENAI_FIRST_BYTE_20S`, stream creation, first chunk and the provider's first-event guard), since gpt-5-nano needs about 4s median and up to 5s; NVIDIA 5s, where key rotation exists. Gaps between chunks: 5s for all. |
| HTTP 410 | Skip to next model. |
| 429 | Rotate NVIDIA keys; wait on true quota after rotation; do not hop providers for 429. |
| OpenAI auth | Env `OPENAI_API_KEY` → SQLite `openai:default`. Never pin NVIDIA profiles on OpenAI sessions. |
| NVIDIA auth | `nvidia:default` → `key2` → `key3`, balanced. |
| OmniRoute | Not in the live Slack path. |
| Decision model | `jev-1.13.0` (TypeSafe), pinned. Typed intake and memory-promotion decisions only; never writes text. `TYPESAFE_API_KEY` in `/etc/openclaw/openclaw.env`. Modes in `settings.json` `jev`; `AURA_JEV_MODE=off` disables both. |

---

## 9. Anti-patterns (do not reintroduce)

1. Greeting allowed/forbidden word lists; crawler/TOOLS.md dump bans.
2. Keyword/`process_type_hint` hard-routing after or instead of intake.
3. Injecting a WEB CAPABILITY BRIEF (tool catalog) into chat / RMP-opinion / awareness turns.
4. Native Slack fallback “so the user is not silent.”
5. `execution_mode=conversational` skipping evaluator or `/tasks`.
6. Vector similarity auto-attach.
7. Treating canary/heartbeat as the user’s active task.
8. Raising idle to hide a stuck model.
9. MiniMax- or Kimi- or GLM-as-primary in a second rule file.
10. Declaring production-complete while leaving placeholders, dual MiniMax/gpt-5-nano rules, or `/health` claiming a 410 embedder is ready.
11. The decision model (Jev) as a chat model, an answer-quality judge in place of the Process Evaluator, or a chat-model router.

---

## 10. Contradiction register

| Old claim | Winning claim | Why |
|-----------|---------------|-----|
| “I won’t add OpenAI” (Jun 2026) | gpt-5-nano primary | Latest instruction Sep 2026; NVIDIA chat models were insufficient. |
| MiniMax M3 primary / GLM fallbacks | gpt-5-nano → gpt-oss-20b (NVIDIA); no GLM, no MiniMax | GLM 410, MiniMax 410 since 2026-09-09; keys module. Nested `rmp/.cursor/rules` copy was stale. |
| DeepSeek V4 Flash as last fallback and subagent/evaluator model | `openai/gpt-5-nano` wherever DeepSeek was | Sep 28 2026: `deepseek-v4-flash-0731` reached end of life on Sep 21 (HTTP 410); every evaluator run failed. |
| Disable model fallbacks | Ordered fallbacks required | Idle/410/unavailable models. |
| Ask user at 10 | Strategy change at 10; user diagnosis at 20 | `request_2`. |
| Conversational = Slack-first / skip evaluator | Conversational = short RMP plan, still gated | Aug 12 + Sep 5 stone. |
| Native fallback if `POST /tasks` fails | Fail closed; optional RMP-owned error DM | Sep 5 stone. Integrity-audit plan superseded. |
| Keyword catalog / GENERIC_PROFILES assignment | Advisory at most; intake LLM assigns | “What’s the deal with keywords?” |
| Vector gate auto-path | `vector_gate` returns None | Retrieval is evidence. |
| Greeting locks / crawler bans | Clock fact + dialogue; no content bans | Sep 5 22:52–22:59. |
| `create_fresh` = start with empty mind | New task row; inject dialogue | Sep 5 memory complaint + `request_2` “point out this is new.” |
| Heartbeat should Slack the user | Isolated, suppressed | RMP heartbeat policy. |
| `auth-profiles.json` is the store | SQLite `authProfiles.store` | OpenClaw 2026.9. |
| `nv-embed-v1` is the working embedder | Honest health: working replacement or not-ready | NVIDIA NIM deprecated (HTTP 410). |
| Aura owns Slack because she is independent | Independence ≠ owning delivery | Feb constitution vs RMP era: Aura executes; RMP judges and delivers. |
| Intake decides only through the LLM chain | Jev typed decision first; LLM chain below thresholds | Sep 28 2026: 11 of 14 DMs in 30 days fell to the zero-confidence fallback. |

---

## 11. Pointer map

| Document | Role |
|----------|------|
| **This file** | Constitution (why / must) |
| [`ARCHITECTURE.md`](../ARCHITECTURE.md) | Runtime how (services, files, flows) |
| [`CONTROL_PLANE_PROGRESS.md`](CONTROL_PLANE_PROGRESS.md) | Append-only control-plane execution log |
| [`CONCEPT_TREE_PROGRESS.md`](CONCEPT_TREE_PROGRESS.md) | Append-only log for this constitution work |
| `/root/.cursor/rules/rmp-architecture.mdc` | Thin always-on coding binding |
| `/root/.cursor/rules/openclaw-upgrade.mdc` | Upgrade/patch law |
| `/root/.openclaw/workspace/TOOLS.md` | Aura executor notes (PROCESS BRIEF, tools). Not Cursor constitution. |
| `/root/.openclaw/workspace/USER.md` | Human facts (Kirill, JST) |
| `/root/request/request.txt`, `request_2` | Founding vision |
| `docs/history/*` | Dated history, not live spec |

---

## 12. Residual product risks (not unfinished constitution)

These are the limits this VPS still cannot remove. They are not unfinished code on the Slack path.

- Slack Socket Mode on **other hosts** is undetectable from this VPS. This host expects exactly one `openclaw-gateway`. Never `apps.connections.open`. See [`runbooks/slack-sockets.md`](runbooks/slack-sockets.md).
- Historical day-bucket `nvidia:unknown` totals have no stored model id and were not rewritten.
- Temporal is one official server on this host's Postgres. There is no second node and no Temporal Cloud account here.

Galaxy tools that are advertised run or say they failed. Obscura stays optional. Safe Harbor scanners stay off the DM path. Do not “fix” the leftovers above by violating §4 (raising idle, native Slack, attaching users to canaries, inventing `nvidia:keyN`).
