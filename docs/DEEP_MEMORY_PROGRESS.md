# Deep memory and two-phase answering — progress log

Append-only. One entry per plan step, newest at the bottom. Each entry states what was done, the evidence, the files changed, how it was verified, and any deviation from the plan with the reason.

**Plan:** `/root/.cursor/plans/deep_memory_two-phase_e5953719.plan.md` (approved 2026-09-30).

**Kirill's decisions (2026-09-30):**
- The Internal Agent's (IA) model work calls the OpenAI Responses API directly: gpt-6-luna, `reasoning.effort=medium`, `store=false`.
- No separate reranker. Fast replies use fused hybrid ranking; on the deep path gpt-6-luna reads the candidates.
- Approved deletion: the 43 legacy procedural rows and their vectors, after a JSON backup.
- Declined: workspace re-ingestion, canary retention, historical backfill. Deep memory starts at go-live.

---

## Step 1 — Baseline and research brief

**Date:** 2026-09-30 (11:07–11:25 JST)

**Baseline:**

- Code: `main` at `371c87d`, working tree clean.
- Tests: full suite 596 passed, 3 skipped (137 s). The three skips need `bs4`, which is not installed. The whole-path harness (10 scenarios) and the judgment harness (11 scenarios) are part of the suite.
- Live readiness: 30 pass, 1 warn (telemetry: no OTLP endpoint, by design), 0 fail. All six invariants pass (`judged_deliveries`, `attached_messages`, `slack_delivery`, `orphan_recoveries`, `memory_hygiene`, `vector_sync`).
- Data: 183 user tasks; 3,893 `task_messages` (3,635 canary); 3,760 active `memory_items`; 214 registry entries; 14 artifacts; 0 `memory_links`. Qdrant 1.17.0, qdrant-client 1.17.1, openai 2.28.0. Host: 4 CPUs, 7 GB RAM, no GPU.
- Latency, last 20 delivered user tasks, measured from task creation, which comes before intake (script `/tmp/audit/latency_baseline.py`, read-only):

  | Stage | Median | p95 | Max |
  |---|---|---|---|
  | Creation → workflow start (intake + API memory prefetch) | 7.0 s | 109.4 s | 155.7 s |
  | Workflow start → Aura's first prompt (plan + memory build) | 5.1 s | 14.5 s | 16.1 s |
  | Creation → Aura's first prompt | 13.0 s | 56.7 s | 171.8 s |
  | Creation → first Slack delivery | 40.1 s | 1,258 s | 1,367 s |
  | Aura's first prompt size | 12,754 chars | 14,761 chars | 16,645 chars |

  The first-reply tail is the reworked long tasks (Kubernetes guide, 23 min).
- Prompt duplication confirmed live. In task `88035ce7` ("What's my test code word?"), the first memory item appears at character 966 and again, identically, at 5,134 of an 11,064-character prompt. It holds 10 `[procedural]` lines and 6 `[semantic]` ones.

**Research brief (sources):**

- **Hybrid search in Qdrant 1.17** ([hybrid queries](https://qdrant.tech/documentation/search/hybrid-queries/), [server-side BM25](https://qdrant.tech/documentation/inference/inference-bm25/), [tuning](https://qdrant.tech/articles/how-to-tune-hybrid-search/)).
  - BM25 sparse vectors are computed on the server from `models.Document(text, model="qdrant/bm25")`. The sparse vector config needs `modifier=IDF`.
  - Dense and sparse prefetches are fused with `RrfQuery(rrf=Rrf(k, weights))`. Qdrant's RRF constant defaults to k=2, not the classic 60. Per-prefetch weights need 1.17+.
  - DBSF is the alternative fusion. The choice should come from a labelled set, and our eval in Step 15 compares them.
- **Contextual retrieval** ([Anthropic](https://www.anthropic.com/engineering/contextual-retrieval), [cookbook](https://platform.claude.com/cookbook/capabilities-contextual-embeddings-guide)).
  - A model writes 50–100 tokens that situate each chunk in its document. The header is prepended before both embedding and BM25 indexing.
  - Top-20 retrieval failures fell 35% with contextual embeddings, 49% adding contextual BM25, and 67% adding a reranker. Top-20 beat top-10 and top-5.
  - Cost stays low because the document is shared across the chunk calls. We batch all chunks of a section into one structured call.
- **Hierarchical agent memory.**
  - xMemory ([arXiv 2602.02007](https://arxiv.org/pdf/2602.02007v1.pdf)) builds messages → episodes → semantic facts → themes. It retrieves top-down and admits finer evidence only when it reduces the reader's uncertainty.
  - MemoryLACE ([arXiv 2609.03201](https://arxiv.org/pdf/2609.03201)) keeps active, superseded and conflicting memories distinct. It reranks relation-connected memories as one unit, and packs update chains in order.
  - LiCoMemory ([ACL 2026 Findings](https://aclanthology.org/2026.findings-acl.1835.pdf)) treats the graph as a semantic index linked to source text, with temporal reranking.
  - Zep/Graphiti ([arXiv 2501.13956](https://arxiv.org/abs/2501.13956)) is a bi-temporal fact graph: facts carry validity windows and are invalidated, not deleted. It reranks with RRF, MMR and graph distance.
- **Fact consolidation** ([Mem0 comparison](https://www.digitalapplied.com/blog/open-source-agent-memory-mem0-letta-zep-compared), [Graphiti README](https://github.com/getzep/graphiti/blob/main/README.md), [Mem0 PR 4911](https://github.com/mem0ai/mem0/pull/4911)).
  - Classic Mem0 decides ADD, UPDATE, DELETE or NOOP per candidate against its nearest memories.
  - Mem0 v3 (April 2026) is add-only, with links and time-aware ranking, and captures transitions ("switched from A to B").
  - Graphiti closes the old fact's validity window.
  - Our design follows Graphiti: set `valid_to`, `supersedes_memory_id`, and `supersedes`/`contradicts` links. Superseded facts stay queryable for history, and statements record the transition.
- **Rerankers and embeddings** ([reranker comparison](https://particula.tech/blog/reranker-models-compared-cohere-voyage-jina-bge-latency-ndcg), [embedding leaderboard](https://awesomeagents.ai/leaderboards/embedding-model-leaderboard-mteb-march-2026/)).
  - Hosted rerankers cost about 600 ms and a new vendor. CPU-only self-hosting is slow, and Jina v3 is non-commercial. Decision: gpt-6-luna reads on the deep path.
  - `text-embedding-3-large` (MTEB 64.6) keeps most of its quality when shortened to 1536 dimensions, which beats `3-small` (62.3) at the same size.

**Files changed:** `docs/DEEP_MEMORY_PROGRESS.md` (new).

**Verification:** the baseline numbers above come from the live system and the full suite.

---

## Step 2 — Direct model client and memory lane

**Date:** 2026-09-30 (11:25–12:05 JST)

**Research before coding:**
- Responses API streaming (typed SSE events; `response.completed` carries the output and usage) and strict JSON-schema output ([structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)).
- NVIDIA NIM structured generation for gpt-oss-20b ([NIM](https://docs.nvidia.com/nim/large-language-models/latest/structured-generation.html), [container notes](https://docs.nvidia.com/nim/large-language-models/1.15.0/nim-container-variants.html)): `guided_json` or `response_format`, depending on the serving backend.

**Live probe** (realistic enrichment prompt, 7,000-char section, 5 chunks):
- **gpt-6-luna**, medium effort, strict schema, `store: false`: first event 0.7–1.1 s, largest gap 1.0–1.5 s, 6–8 s total, valid output. Prompt caching works without storage: the second call reused 2,265 cached tokens.
- **gpt-oss-20b on NVIDIA:** `response_format: json_schema` gave valid output (first event 0.6 s, 9.6 s total). `guided_json` was ignored, and the output failed the schema.

**What changed:**
- `app/llm/openai_direct.py` (new): `structured_call(schema, *, purpose, instructions, input_text, priority, deadline_sec, max_output_tokens)`.
  - It streams the Responses API (`gpt-6-luna`, `reasoning.effort=medium`, `store: false`, strict schema from the SDK's `to_strict_json_schema`) and validates the answer with pydantic.
  - Two OpenAI attempts (no second one after a non-retryable error), then one NVIDIA attempt with `response_format: json_schema`, pacing its key through `wait_for_dispatch_sync(deadline=…)`.
  - Rule 3 timeouts:
    - OpenAI gets 20 s to its **first output** event. Lifecycle events (`response.created` and `response.in_progress`) arrive before reasoning, so timing from them would count the reasoning period as a gap.
    - Then 5 s between events.
    - NVIDIA gets 5 s for both.
    - A per-call deadline (default 120 s) covers the lane wait and all attempts.
  - One circuit breaker per provider and process, following the Jev client's pattern: 3 outages, or a 429 for max(30 s, retry-after). Invalid output and refusals do not count as outages. OpenAI 429s never touch the broker's `record_rate_limit`, which would cool Aura's `openai:default` profile in OpenClaw. NVIDIA 429s use it as the embedder does. HTTP 410 is reported as `model_gone`.
  - A data rule is prepended to the instructions, and output is capped at 400,000 characters.
  - The client is per event loop, closed at API shutdown and in the worker's `finally`.
- `app/llm/quota_broker.py`:
  - `reserve_memory_lane_slot` and `release_memory_lane_slot` keep the lane in the broker's file-locked state, so it is shared by the API process (enrichment) and the worker (recall).
    - Enrichment is not admitted while a recall waits.
    - Enrichment holds at most `busy_enrich_slots` while user runs hold broker slots.
    - A per-minute cap applies across processes.
    - Slots older than 10 minutes (a dead process) are reclaimed.
  - The lane appears in `get_orchestration_status()["memory_lane"]`.
  - `wait_for_dispatch_sync` takes an optional `deadline`.
- `app/llm/usage_monitor.py`:
  - New source `memory_llm`.
  - `source_tokens_today(source)` backs the daily token budget.
  - `transcript_usage()` reports `direct`, the ledger's direct-call totals for its window, and `usage_alerts()` adds them to the 24 h prompt budget (and to the "still burning" check). They are kept out of the abort rate, since direct calls are not OpenClaw attempts.
- `app/api/server.py`, `worker.py`: close the direct client on shutdown.
- `tests/test_openai_direct.py` (new, 14 tests; httpx `MockTransport` with timed SSE streams) covers:
  - a structured success, with the request shape, ledger row and lane release checked;
  - reasoning silence inside the first-output budget;
  - no first output → NVIDIA;
  - an idle gap after output started;
  - invalid output → retry → fallback, with no circuit opened and both calls billed;
  - a non-retryable 400 skipping the second attempt;
  - both circuits opening and then failing fast;
  - an OpenAI 429 honouring retry-after without cooling OpenClaw's profile;
  - NVIDIA 410;
  - the daily budget refusing before any request;
  - lane priority and the busy-user cap;
  - the per-minute cap and stale-slot reaping;
  - lane timeout;
  - direct tokens in the 24 h budget.

**Deviation:** the plan's "lane limits, budget" settings are part of Step 3's `deep_memory` section. Until then, `lane_policy()` returns the dataclass defaults: concurrency 3, 60 calls per minute, 1 enrichment slot while users run, 4M tokens per day, 120 s per call.

**Verification:**
- Full suite: 610 passed, 3 skipped (596 + 14 new).
- Live: one `structured_call` per model on a real snippet.
  - gpt-6-luna: 1 attempt, 3.3 s, correct facts and code word.
  - Forced fallback (OpenAI circuit held open): gpt-oss-20b, 1 attempt, 3.4 s, correct. NVIDIA returned real usage, so `stream_options.include_usage` is supported.
  - The ledger went from 0 to 697 `memory_llm` tokens, and the lane showed 0 active and 2 calls in the last minute.

**Deployment note:** `rmp-code-watch` restarts `rmp-api` and `rmp-worker` when `app/**/*.py` changes and no user task is active. It restarted them at 11:25 and 11:28 JST on these edits, which are backward compatible (new functions; the budget adds zero until the IA calls). From Step 3 on, development moves to a git worktree, and each step reaches the live tree only after its tests pass.

---

## Step 3 — Conversation-log metadata, deep-memory tables, settings

**Date:** 2026-09-30 (12:05–12:45 JST). Developed in the worktree `/root/.openclaw/rmp-deep` (branch `deep-memory`).

**What changed:**
- **`task_messages`** gains three columns:
  - `kind`: `request`, `attached`, `clarify_answer`, `reply`, `followup`, `notice` or `verdict`;
  - `session_key`;
  - `meta`: Slack message, thread, reply-to and attachments, intake decision, process run, attempt.

  Every writer sets them:

  | Writer | Kind | Meta |
  |---|---|---|
  | `create_task` | `request` | Slack context, intake decision, task type |
  | The signal endpoint | `attached` | signal type (session taken from the task row) |
  | Intake attach | `attached`, or `clarify_answer` when it answers a clarify question | Slack, decision, targets |
  | Intake clarify and skip | `request` | Slack, decision, clarify or skip kind |
  | `send_slack_message_idempotent(kind, session_key, meta)` | `reply` from `EvaluatorRetry._deliver_final` (generic and catalog); every other `notify_slack_user` call is RMP's own text and stays `notice` | process run, parts |
  | Evaluator rows | `verdict` | verdict, attempt, process run |

  Empty metadata values are dropped.
- **New tables** in `app/db/models.py`:
  - `dm_documents`: kind, a unique `source_key`, summary, TOC, status, `source_at`, `valid_to`;
  - `dm_sections`: TOC entries with path, level, summary;
  - `dm_chunks`: text, `context_header`, TOC pointer (`section_id`), task, session, process run, message, role, `source_at`, `meta`, `valid_to`;
  - `dm_links`: typed edges, unique per source, target and relation;
  - `dm_ingest_queue`: a partial unique index deduplicates pending work per source, and a partial due-index serves the drain;
  - `dm_context_reports`.

  Section and chunk ids are assigned by the ingester (deterministic in Step 5), so facts can cite chunks that survive re-ingestion.
- **`app/db/database.py`:** idempotent DDL for the three `task_messages` columns and their indexes, plus English full-text GIN indexes on chunks (header + text), sections (title + summary) and documents (title + summary). The new tables come from `create_all`.
- **`app/config.py`:** `DEFAULT_DEEP_MEMORY`, merged in `load_settings()`, with `get_deep_memory_config()`. The section holds:
  - kill switches `enabled` (ingestion, index, fast context), `recall_enabled` and `followups_enabled`;
  - the collection name, `text-embedding-3-large` at 1536 dimensions;
  - lane concurrency, per-minute cap and busy-enrich slots;
  - the daily token budget and call deadline;
  - ingest concurrency and the tool-document threshold;
  - fast-context deadline and size;
  - recall deadline and follow-up wait.

  `openai_direct.lane_policy()` now reads these settings, falling back to its defaults on invalid values. `settings.example.json` carries the section.

**Deviation:** the plan names two kill switches, `enabled` and `followups_enabled`. A third, `recall_enabled`, separates recall, whose report the evaluator and later steps can use, from the follow-up message. Both default to off until Step 15's live acceptance turns them on, so no step before that changes what Kirill sees.

**Verification:**
- 75 related tests pass, and the full suite has 617 passed, 3 skipped (7 new).
- New whole-path scenario: an ask and an attached follow-up log `request`, `attached`, `notice` (the attach note), `verdict` and `reply`, all on the Slack session. The Slack ids, intake decision, process run and verdict are in the metadata.
- Unit tests cover: the pending-job index rejects a duplicate and accepts the same source once the first is done; empty metadata is dropped; the migrations list; settings merge and example parity; `lane_policy` reads settings and falls back on invalid values.
- Live Postgres, in one transaction that was rolled back: two full rounds of `create_all` plus all 52 migrations succeeded, and created the 3 columns, the 6 tables and all indexes. Afterwards 0 `dm_*` tables existed.

---
