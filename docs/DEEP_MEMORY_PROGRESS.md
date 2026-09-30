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

**Deployed** 12:48 JST (`7ada166`). The watcher restarted both services, and startup created the 6 tables and 3 columns on the production database. The API was healthy, with no startup errors.

---

## Step 4 — Hybrid index; user memory moves into it

**Date:** 2026-09-30 (12:50–14:20 JST)

**Research before coding:**
- Qdrant server-side BM25 ([inference](https://qdrant.tech/documentation/inference/inference-bm25/)), payload index types and ordering ([indexing](https://qdrant.tech/documentation/manage-data/indexing/)). Payload indexes should exist before points, so that HNSW adds filter-aware edges.
- A scratch probe on this Qdrant 1.17 (collection deleted afterwards):
  - `models.Document(text, model="qdrant/bm25")` works self-hosted for upserts and queries, and accepts `options={"language": "english"}`;
  - IDF-weighted BM25 found the exact "PELICAN-47" document first;
  - `RrfQuery(rrf=Rrf(k=2, weights=[…]))` fuses dense and BM25 prefetches.

**What changed:**
- `app/deep_memory/index.py` (new; see the deviation below):
  - **Collection** `rmp_deep_memory_v1`: named dense vector (`text-embedding-3-large` at 1536 dimensions) plus sparse `bm25` (IDF). `ensure_collection()` refuses an existing collection with other vectors, and creates the 10 payload indexes (level, ref_id, document_id, task_id, session_key, source_kind, scope_id, memory_type, valid, source_at) before any point.
  - **Embedding:** `embed_texts` batches 64 inputs per request, orders the results and records usage as `embed`. The ledger had no embedding rows before this. `embed_query` keeps a thread-safe single-flight cache with a 5-minute TTL, so the six scope reads of one memory block embed the query once.
  - **Writing:** `upsert_points` writes the dense vector and a server-side BM25 document.
  - **Search:** `search()` prefetches dense and BM25 candidates under one filter (level, validity, exact and any-of matches, a time range on `source_at`), then fuses them with weighted RRF.
  - **Points from the record:** `build_points(db, refs)` turns refs (`chunk:`, `section:`, `document:`, `fact:<id>`) into points from the Postgres rows, or None when a row is gone, retired, has no summary, or is not user memory (the caller then deletes the point). Chunk points embed the contextual header together with the text, and carry the TOC pointer (`section_path`), the conversation metadata and the source time.
  - **Fallback:** `fts_search()` is Postgres text search over the same objects, with OR semantics (`websearch_to_tsquery('english', 'a or b …')` for dm tables, `simple` for facts).
- `app/memory/vector_sync.py`:
  - Outbox kind `deep`, drained in batches with one embedding request per 64 points. While deep memory is off these rows are left out of the query, so they cannot crowd out other work.
  - A legacy `memory` row for a user-scope item only removes its legacy point.
  - The reconcile covers the new collection: every valid chunk, summarized section and document, and user fact is expected there, with the same orphan guard. The legacy collection now expects only process and procedural memory.
  - Drain batch 50 → 100.
- `app/memory/router.py`:
  - User-scope writes queue `deep` rows, and user-scope semantic search asks the new index for this user's facts.
  - If the index cannot answer (collection missing, disabled, timeout, error), Postgres text search answers instead, as before.
  - `read()` now filters `valid_to IS NULL` (finding: superseded rows were read).
  - Episodic compaction also removes user points from the new index.
- `app/memory/vector.py`: a failed embedder probe is retried after 60 s instead of staying failed until a restart, and `status()` re-probes after that time.
- `app/production/invariants.py`: `vector_sync` counts drift in the new collection too.

**Deviation:** the plan gave the index adapter to a Grok 4.7 extra-high subagent, and the chunking module (Step 5) to a second one. Both started, but neither did anything in 11 to 16 minutes; their transcripts held only the prompt. The previous audit met a Grok provider usage limit until 2026-10-01 00:00 UTC. Both subagents were told to stop, and I wrote the adapter to the same spec.

**Verification:**
- Tests:
  - New `tests/test_deep_index.py` (13 tests: fake Qdrant plus a real SQLite schema).
  - `tests/test_vector_sync.py` has 4 tests updated for the new split and 7 new: deep batch and retry, deep rows held while off, deep reconcile, user search through the new index, superseded rows never read, probe retry.
  - Full suite: 635 passed, 4 skipped.
- New opt-in `tests/test_deep_index_integration.py` (`RMP_QDRANT_IT=1`) against the real Qdrant, through the shared client, which prefers gRPC, on a throwaway collection: hybrid ranking, level and validity filters, match, scroll and delete. Passed.
- `fts_search` ran read-only against production Postgres and returned OR-matched rows.

**Deployed** 14:17 JST (`5ae9b2b`), then `ops/reconcile_vectors --apply` as the migration.
- It removed the 51 user-scope points from the legacy collection, and queued and indexed the 51 user-scope rows in the new one (0 failures).
- A dry-run reconcile afterwards: legacy 2,552 rows = 2,552 points; registry 214 = 214; new collection 51 objects = 51 points; nothing missing, no orphans.
- A live `/memory/lookup` (user, semantic) returned vector hits from the new index.
- This was index migration only. There was no model enrichment and no backfill.

---

## Step 5 — Ingestion stage 1: documents, tables of contents, chunks (no model)

**Date:** 2026-09-30, deployed 12:47 JST (`70073ce`).

**Correction to the entries above:** the clock times in the Step 3 and Step 4 entries were wrong. The reload log (UTC) shows the Step 3 deploy at 11:48 JST and the Step 4 deploy and migration at 12:17 JST, not 12:48 and 14:17. From here on, times come from the system clock.

**What changed:**
- `app/deep_memory/chunking.py` (new), structure-aware chunking:
  - ATX and setext markdown headings become the TOC, with `A > B > C` paths. Headings inside fenced code are not headings; `---` after a blank line is a thematic break, not a heading.
  - Headings with no text are dropped but stay in their children's paths.
  - Titles are cleaned of emphasis, trailing colons, and the anchor links page extractors glue onto headings.
  - Chunks of about 1,400 characters: blocks are packed greedily, fenced code stays whole, an oversized block splits at sentences and then at words, a tiny tail is merged, and a 200-character overlap starts on a word boundary.
- `app/deep_memory/ingest.py` (new), stage 1:
  - **Queue:** `enqueue()` writes `dm_ingest_queue` in the writer's transaction with `ON CONFLICT DO NOTHING`, so a pending duplicate never breaks the message write. It is fed by:
    - `add_task_message`: Kirill's `request`, `attached` and `clarify_answer` turns;
    - `send_slack_message_idempotent`: Aura's `reply` and `followup` turns (RMP notices are not turns);
    - `index_terminal_task_async`: finished tasks.
  - **Drain:** `deep_ingest_loop` runs in the API lifespan next to the vector outbox. It claims jobs with `SKIP LOCKED` and a 5-minute lease, then runs each job in its own transaction, so a failure leaves nothing half-written and retries with backoff.
  - **Turn:** a `Kirill:` or `Aura:` chunk in the task document's Conversation section. It carries task, session, process run, message, role, source time, kind, Slack ts and intake decision, and is queued for the index at once.
    - A reply of 2,400+ characters or with two headings becomes a **deliverable** document with its own TOC; the conversation keeps a pointer chunk to it (title, size, section titles, opening).
    - Rows written before kinds existed are read as `request` or `reply`, so a task spanning the deploy keeps its early turns.
  - **Task:** at task end the document gets its **Path history**, **Deliverables** and **Actions** sections, and its TOC.
    - Path history: status and closed reason, JST times and duration, the intake decision with confidence, rationale and related tasks, plan steps, each evaluator verdict per attempt, attached messages, deliveries, escalations and failures.
    - Actions: every tool call, with ok or failed.
    - Pages and files Aura read (8 reading tools, results of at least `tool_document_min_chars`) become **tool documents**. `readable_result()` takes the page out of `web_fetch`'s JSON envelope and OpenClaw's `EXTERNAL_UNTRUSTED_CONTENT` markers (security preamble dropped, real page title kept). Web pages are flagged `untrusted`, and identical pages are kept once.
    - Lineage: `part_of` links from deliverables, tool documents and attachments to the task; `related` or `continues` links to the tasks its intake decision named.
  - **Attachment:** plain-text files, only from OpenClaw's inbound media directory (no path escapes) and up to 2 MB, become documents keyed by content hash and linked to the task.
  - Ids are derived from sources (uuid5), so ingesting again changes nothing, and a shrinking unit retires its old chunks, which queues their index deletion.
  - Canary, heartbeat and cron runs, internal intents and intake placeholders never enter.
- `app/openclaw_sessions.py`: `task_tool_results()` returns the full results of named tools, secrets redacted.
- `tests/conftest.py`: an autouse fixture makes the deep index unreachable in tests (no live Qdrant, no real key), except the opt-in integration test. This fix came from the whole-path harness catching an unstubbed search. The harness stubs the new index as a boundary, like the legacy one.

**Deviation:** I wrote the chunking module myself (see Step 4 on the Grok subagents).

**Verification:**
- `tests/test_chunking.py` (12) and `tests/test_deep_ingest.py` (12) cover:
  - a pending job queued once, and which writers queue turns;
  - a turn chunk with its metadata, idempotent on a second run;
  - a long reply as a deliverable with its TOC, pointer and link;
  - a fixture task's whole tree: sections, path-history facts, the actions digest, a page as a deduplicated tool document, lineage;
  - a shrinking unit retiring its chunks;
  - canary, heartbeat, cron and placeholders excluded;
  - attachments: text read; image, oversized, outside-path and `..` escape refused;
  - the drain: done, failed with backoff, no partial rows;
  - page-envelope extraction.
- Full suite: 659 passed, 4 skipped.
- Real data, ingested against production Postgres inside a rolled-back transaction (0 documents afterwards). For the Kubernetes-guide task:
  - the 27,854-character reply became a deliverable with 15+ TOC sections;
  - the task document got Conversation, Path history and Actions;
  - its 4 fetched pages became clean tool documents titled "Releases | Kubernetes", "Manual Upgrades | K3s" and so on;
  - 5 `part_of` links and 73 index rows.
- Live: the watcher restarted both services at 12:47:56 JST, health is OK, and the ingest loop is claiming every 10 s. The queue stays empty until the next real message, because there is no backfill.

---

## Step 6 — Ingestion stage 2: summaries, contextual headers, task summaries, registry fixes

**Date:** 2026-09-30 (12:50–13:35 JST)

**Research before coding:**
- Anthropic's contextual-retrieval recipe (cookbook prompt: "a short succinct context to situate this chunk within the overall document for the purposes of improving search retrieval"). It uses one call per chunk with the document cached, costing about $1.02 per million document tokens.
- Ours batches all chunks of a section into one structured call, with the document's outline as the shared context. This gives the same kind of header with far fewer calls and a section summary at no extra cost. Summaries are hierarchical: the document summary is built from its section summaries.

**What changed:**
- `app/deep_memory/enrich.py` (new), `enrich_document(db, document_id)`:
  - **Input:** the document's kind, title, source (URL, path or file), trust ("external web content" for pages), the task it was read or written during, and the JST date, plus the outline with the current section marked.
  - **Per section:** one call through `openai_direct.structured_call` (gpt-6-luna, medium, `priority="enrich"`) returns a summary of 2–4 sentences with names, numbers and dates, and a 50–100-token context header for every numbered chunk. Batches hold at most 24 chunks. An answer missing any chunk is rejected, so nothing is written and the job retries.
  - **Per document:** the summary comes from the section summaries; a single-section document reuses its section summary without another call. A task document gets `TaskEnrichment`: summary, **outcome** and **answer** (the substance of what Aura said, 120 words at most), from the goal, the section summaries and Aura's final reply.
  - **Model calls run outside any database transaction.** Headers are written only to chunks whose text is unchanged since they were read. A rewritten chunk loses its header during stage 1, and its section, document and registry entry are enriched again.
  - **Writing:** section summaries, chunk headers, the document summary, TOC summaries, `status=enriched`, and `meta.outcome`/`meta.answer`. It queues index rows for the changed chunks (re-embedded with header plus text for dense and BM25), the sections and the document (their own index levels), and, for a task, the registry entry.
  - Enriching again is a no-op: no model call, nothing queued.
- `app/deep_memory/ingest.py`: new job kind `enrich` (priority 4), queued for a task's document and its deliverables, pages and attachments when the task ends, and for an attachment once it is read. A job that hits the daily token budget is **deferred to the next UTC day** without counting as a failure.
- Registry:
  - `summary.build_task_summary` leads `outcome_summary` with the enriched task's outcome and answer, then the status tokens (cap 1,200 characters). Intake's finished-task evidence and `create_guided`'s SIMILAR COMPLETED TASKS now carry the answer, not just "status=completed; evaluator=accept".
  - `indexer.index_terminal_task` writes the Postgres entry **before** the vector. A failed vector write still raises so the outbox retries, but the entry already exists.
  - `retriever.fetch_recent_registry` counts "recent" from `task_ended_at` (not `indexed_at`, which every re-index refreshed) and excludes canary, heartbeat and cron.

**Verification:**
- `tests/test_deep_enrich.py` (11, model mocked at `structured_call`) covers:
  - a task document: every schema, headers, summaries, outcome and answer, TOC summaries, index and registry rows;
  - idempotent re-enrichment;
  - a rewritten chunk re-enriched with its section;
  - an incomplete answer writes nothing;
  - a chunk rewritten during the call keeps no stale header;
  - a single-section page (trust and source in the prompt);
  - 24-chunk batching;
  - budget deferral;
  - the registry leading with outcome and answer;
  - a final reply from before message kinds;
  - recent-registry semantics.
- The registry indexing test was updated for Postgres-first.
- Full suite: 670 passed, 4 skipped.
- **Live enrichment** against production Postgres, rolled back, with real gpt-6-luna calls (43,709 `memory_llm` tokens, about a cent). All 51 chunks of four documents got headers.
  - "What's my test code word?": summary "…Answer: PELICAN-47"; chunk header: "Kirill's question in the Conversation section of the task record titled 'What's my test code word?', dated September 30, 2026…".
  - The Kubernetes task record: summary with the outcome (guide delivered, 17 sections, 06:26 JST).
  - The 27,854-character guide (31 chunks, 37 s): a summary with the substance (K3s, three embedded-etcd servers tolerate one outage, 8–16 GB RAM per node).
  - The fetched releases page (9 chunks, 14 s): "…branches 1.37, 1.36, 1.35… 1.34.12 released 2026-09-15, end of life 2026-10-27".
  - The registry text for the code-word task: "Outcome: … PELICAN-47 identified as the test code word. Answer: PELICAN-47 [status=completed; slack.delivered; evaluator=accept; …]".
  - The run showed one bug, fixed and tested: a final reply logged before message kinds existed was read as "(none)".

---

## Step 7 — Facts, consolidation, promotion rework, legacy procedural delete

**Date:** 2026-09-30, deployed 13:27 JST (`6cf8d85`). **Correction:** Step 6 was deployed at 13:03 JST. Its "12:50–13:35" range was an estimate.

**Research before coding:**
- Mem0's extraction prompt ([prompts.py](https://github.com/mem0ai/mem0/blob/4debc58a/mem0/configs/prompts.py)):
  - categories: preferences, personal details (names, relationships, dates), plans and intentions, activity preferences, health, professional details, and facts from content the user shares;
  - it extracts from both user and assistant messages and returns an empty list when nothing qualifies.
- Consolidation follows the Step 1 brief: Mem0's per-candidate ADD/UPDATE/DELETE/NOOP against the nearest memories, Graphiti's invalidation instead of deletion, and Mem0 v3's transition capture.

**What changed:**
- `app/deep_memory/facts.py` (new), job kind `facts` (priority 3):
  - It runs only when the task has a delivered reply, and never for internal runs.
  - **Extraction:** one gpt-6-luna call over the numbered turns, both Kirill's and Aura's, returns standalone third-person statements. Each carries a subject, kind, speaker, source turns, `valid_from` and confidence.
  - **Filtered before review:** confidence below 0.6; anything the redactor would change; natural-language credentials (password, passcode, API or access key, auth token, OTP and 2FA codes, card or account number, IBAN, CVV, seed or recovery phrase, and a capitalised PIN). A test code word to remember is not a credential.
  - **Citations:** facts cite the deterministic id of stage 1's first turn chunk, so they can be written before or after ingestion.
  - **Consolidation:** the nearest active facts come from the hybrid index, or Postgres text search when Qdrant cannot answer. One call judges every candidate against them:
    - `same`: nothing is written;
    - `update`: a new fact with `supersedes_memory_id`, the old one retired (`valid_to`, its index point queued for deletion), and a `supersedes` link;
    - `contradicts`: both kept, plus a `contradicts` link;
    - `new`.
  - A decision naming a nonexistent fact counts as `new`.
  - **Review:** Jev reviews everything to be written in its configured promotion mode (currently shadow).
- `MemoryRouter.write` takes an optional `db`, so facts join the job's transaction, plus `valid_from` and `supersedes_memory_id`.
- `app/memory/promotion.py`:
  - The heuristic `extract_semantic_facts` is removed. It produced "Site referenced: …" rows.
  - Promotion queues fact extraction, never writes user or pinned memory itself, and records a procedure only when more than one plan step ran **and** tools were used.
  - `validate_fact` stays for `ops/jev_eval.py`.
- `app/workflows/generic_task.py` and `catalog_task.py`: promotion runs only after the reply was delivered. Catalog workflows used to promote before posting. The change is behind `workflow.patched("deep-memory-promote-after-delivery")`, so histories recorded before it replay unchanged.
- `ops/purge_legacy_procedures.py` (new): dry run by default; `--apply` backs up, then removes the rows' vectors, including points that name their row only in the payload, and the rows.
- Tests:
  - `tests/test_deep_facts.py` (8) covers:
    - a changed code word leaves exactly one active fact (supersede, retire, link, index rows, consolidation input);
    - citations equal stage 1's chunk ids;
    - `same` writes nothing, and a contradiction keeps both;
    - credentials and weak facts never reach review or memory;
    - Jev enforce holds before any write;
    - nothing without a delivered reply or from internal runs;
    - a decision pointing nowhere counts as new;
    - credential detection spares ordinary facts.
  - Tests of the removed heuristic path were rewritten for the new behaviour, and Jev's hold-before-write contract moved to the facts tests.

**Verification:**
- Full suite: 676 passed, 4 skipped. One earlier run had a teardown error in the whole-path reconciler-nudge scenario that did not reproduce in four later runs (three harness runs, one full suite). Future full runs capture tracebacks to diagnose it if it returns.
- Live, with real gpt-6-luna calls and Jev's shadow review, on production Postgres, rolled back:
  - a task saying "Remember this for later: my test code word is ORCA-19." became the fact "Kirill's test code word is ORCA-19.";
  - a later task, "Update: my test code word is now LYNX-44.", was judged an update: "Kirill's test code word is LYNX-44 (changed from ORCA-19).", with `supersedes`, the old fact retired and a `supersedes` link;
  - **exactly one active code-word fact.**
- **Approved deletion applied** at 13:27 JST:
  - 43 legacy procedural rows, all old reply text in the `user` pool, deleted with 82 vector ids;
  - backup `data/backups/purge-legacy-procedures-20260930T042732Z.json` (0600, 43 rows);
  - one procedural row remains, the only real procedure summary;
  - 25 more legacy points named deleted rows only in their payload (confirmed by scan, all procedural). `ops/reconcile_vectors --apply` removed them, and the script now finds such points itself.
  - Afterwards: legacy 2,509 rows = 2,509 points, registry 214 = 214, deep 51 = 51.

---

## Step 8 — One fast context, assembled once

**Date:** 2026-09-30, deployed 14:40 JST (`b7c1625`).

**Calibration first (live, read-only):** dense cosine of `text-embedding-3-large` at 1536 dimensions, for the top facts of five real questions.
- Relevant: 0.49–0.58 (the Kubernetes pages for a Kubernetes question).
- Borderline: 0.31–0.33 (USER.md for "who am I").
- Noise: 0.15–0.30 (heartbeat notes, 0.299, for "test code word", which has no matching fact).
- BM25 raw scores are no relevance signal on their own: an irrelevant note scored 8.94 on shared words.

So the fast path's floor applies to dense similarity, at 0.30 (`fast_context_fact_floor`). BM25 then only reorders what clears it.

**What changed:**
- `app/deep_memory/curator.py` (new), `assemble_fast_context`: one block, headed `PROCESS-SCOPED MEMORY` as the prompt policy, plugin and canary expect. Sections in priority order:
  1. **RECENT DIALOGUE** of this Slack conversation, which now includes catalog-typed user tasks; it keeps its newest turns when cut.
  2. **Facts** from the deep index above the floor (`index.search(dense_floor=…)`: a dense query with `score_threshold` picks the candidates, then the fused dense+BM25 query ranks only those), plus pinned facts. Superseded facts never appear. If the index cannot answer, Postgres text search is used.
  3. **Earlier tasks intake linked** (the decision's `similar_task_ids`): their enriched summary with outcome and answer, else their registry entry. Internal tasks never appear.
  4. **This run so far**: working and episodic memory, by recency.
  5. **Up to 3 procedures** above a similarity floor.
  - Budget and deadline: shares of 2,600, 1,400, 1,200, 1,200 and 600 characters within `fast_context_max_chars` (6,000), and a 3 s deadline (`fast_context_deadline_sec`). Legs run concurrently, and a leg that misses the deadline is left out; the lowest-priority sections go first when space runs out.
  - Canaries, heartbeats and cron get only their own run's memory.
  - Every assembly records `memory.fast_context` (ms, sections, characters), which makes the p95 measurable.
  - `warm_up()` opens the index, embedder and Mem0 clients in the worker at startup, and embeddings reuse one OpenAI client per key.
- The activity `build_process_memory_context` (same name, so replays are unaffected) now returns this block. It serves the generic plan loop, catalog attempts and `resume_clarify`, which previously started with no dialogue or memory.
- `create_task` no longer prefetches memory or dialogue. Its brief is intake's brief plus the web-capability brief. This removes the duplicate memory block found in Step 1.
- The evaluator judges against the same composed block: `process_brief` is now the latest memory block, not the creation-time brief, in both generic and catalog workflows.
  - A draft recovered by the reconciler skips the plan loop, so its block is assembled before judging, behind `workflow.patched("deep-memory-judge-with-fast-context")`.
- `MemoryRouter.read_ordered` no longer reads task-scope memory, which has no writer, or the per-item graph links, which never existed.

**Verification:**
- New `tests/test_fast_context.py` (7) covers:
  - one block in priority order with the right fact-search arguments, and the timing event;
  - text search when the index is down, and no facts section when nothing is close;
  - linked tasks from their summary or their registry entry, never canaries;
  - canaries get only their run;
  - budgets cut low-priority sections, and the dialogue keeps its newest turns;
  - a leg over the deadline is left out while the others arrive;
  - line-safe cutting.
- New whole-path scenario: in two DMs in a row, the second prompt has the memory block once and RECENT DIALOGUE once, with the first exchange, and the evaluator's prompt carries the same dialogue. The prompt policy's own "Use PROCESS-SCOPED MEMORY" instruction is the only other mention.
- The judgment harness registers the memory activity, and its recovered-draft scenario now proves the evaluator sees the dialogue.
- Updated tests: `read_ordered` order without task scope, and the activity-level tests.
- New index tests: the dense floor ranks only close points; the embedding client is reused per key.
- Full suite: 686 passed, 4 skipped.
- **Live**, real tasks on production data, rolled back:
  - For the code-word question: the dialogue with the PELICAN-47 exchange and the run's memory, and correctly no facts section, since nothing is close enough.
  - For the Kubernetes question: dialogue, the relevant documentation facts (0.49–0.57), the run and procedures.
  - Latency: a fresh process without warm-up took 3.0 s and dropped facts at the deadline. After `warm_up()` the first assembly took 307 ms and later ones 197–329 ms.
  - After the deploy, the worker logged "Fast-context clients warm in 4.7s".

---

## Step 9 — Fresh sessions for reworks and verdicts

**Date:** 2026-09-30, deployed 14:59 JST (`3d291c5`).

**What changed:**
- **Session keys:**
  - `send_to_openclaw` takes `session_suffix`, allowed only as `__r<n>` or `__recall` (anything else raises).
  - Rework *n* goes to `rmp_task_<id>__r<n>`; the refinement (Step 13) will use `__recall`.
  - `_execute_on_internal_session(…, verdict=n)` puts verdict *n* in `rmp_verify_<id>__v<n>`, with the NVIDIA fallback in `__v<n>_fb1`. `verify_response_quality` passes the attempt number.
- **Compact brief.** `build_rework_prompt` and `build_strategy_change_prompt` take `memory_block` and `actions`:
  - the request, now up to 8,000 characters (was 1,500);
  - the latest memory block (brief plus fast context; the recall report joins it in Step 13);
  - the evaluator's issues and command;
  - "ACTIONS ALREADY TAKEN IN THIS TASK (reuse what succeeded; do not redo it)", from the new activity `task_actions_digest` (the evaluator's trace format, across all of the task's sessions);
  - the previous draft, now up to 40,000 characters (was 2,000).
- **Workflow.** In `generic_task._judge_and_deliver` the rework fetches the digest, builds the brief and dispatches with `__r<attempt>`, behind `workflow.patched("deep-memory-fresh-rework-sessions")` because it adds an activity call. The worker and both harnesses register `task_actions_digest`.
- **A retried plan step** gets "[<step> attempt <n> not accepted]: <reason>" in its step context.
- **Readers follow the task across sessions.** `openclaw_sessions.task_session_keys` lists the first session, then each rework and refinement, by `created_at`, without the planner's `__plan`. `task_transcript_lines` feeds:
  - `task_action_trace` (evaluator, digest, deep memory's Actions);
  - `task_tool_results` (tool documents; call ids stay raw in the first session and are numbered per session after it);
  - `session_recovery.read_completed_rmp_session_reply`, which takes the latest terminal reply across sessions.
  - The usage monitor and broker already find the task id by pattern search, so the suffixed keys count for their task.

**Deviations:**
- Messages folded into a draft (`AttachedMessages._fold_in`) still go to the task's first session. They are short, and the plan's key list does not include them.
- Catalog workflows keep their own rework path, since the plan names `generic_task.py` for this step.

**Verification:**
- New `tests/test_fresh_sessions.py` (7) covers:
  - sessions found oldest first without the planner, in the SQLite and JSON stores;
  - actions and page reads across the first session and a rework, with repeated call ids;
  - recovery returning the newest session's reply;
  - dispatch keys and refused suffixes;
  - one evaluator session per verdict, including the fallback;
  - the brief carrying the whole request, draft, memory and actions, in order.
- Judgment harness: the rework ran in `__r2`, and its brief carries the memory block, the actions digest, the command and the previous draft.
- Whole-path harness:
  - the rework scenario used session "" then `__r2`, verdict sessions 1 and 2, and one memory block in the rework prompt;
  - a new scenario shows a retried plan step told "attempt 1 not accepted]: Output validation failed".
- Updated evaluator-trace test.
- Full suite: 694 passed, 4 skipped.
- **Live, scratch sessions, nothing delivered:** three real gpt-6-luna turns (medium) on one ask, a first attempt and two reworks with the compact brief. Prompt tokens: first 28,054, rework 2 **24,204**, rework 3 **24,183**, so **flat**. The roughly 24k floor is OpenClaw's system prompt, tools and workspace files. Before, a task's session grew with every attempt: the Kubernetes guide reached about 160k tokens over five reworks. The scratch sessions `rmp_task_12ac088f…` (first, `__r2`, `__r3`) stay in the OpenClaw store.

---

## Step 10 — A stop aborts Aura's in-flight run

**Date:** 2026-09-30, deployed 15:18 JST (`38cd795`).

**Live probes first** (scratch sessions; a five-page `web_fetch` run; nothing delivered):
- The source (`dist/sessions-abort-D87RoCsf.js`): `sessions.abort` takes `{key, runId?, agentId?, clearQueued?}` and answers `{abortedRunId, status: "aborted" | "no-active-run"}`. `clearQueued` applies only to a key-only call.
- `/hooks/agent` returns `{"ok": true, "runId": …}`, but that id is **not** in the gateway's abort registry. Aborting by `runId` answered `no-active-run`, and the run fetched all remaining pages.
- `tasks.list` does not list hook runs either, so `tasks.cancel` cannot reach them.
- **Aborting by key with `clearQueued: true` works:**
  - An abort sent while a fetch was in flight: the fetch finished 1.3 s later, then the run ended with stop reason `aborted` at +4.4 s, with no new tool call.
  - Another run carried out one call the model had already issued at the moment of the abort.

**What changed:**
- `app/openclaw_control.py` (new):
  - `abort_session(key)` runs `openclaw gateway call sessions.abort --json --params {key, clearQueued: true}` and returns aborted, no-active-run, error or timeout.
  - `abort_task_runs(task_id, reason)` aborts every session of the task concurrently (up to 6; each CLI call starts node): Aura's first session, reworks, refinement and planner, and the evaluator's `rmp_verify_…` sessions including fallbacks. It records `openclaw.aborted` (reason, sessions, aborted, unconfirmed).
  - `schedule_abort` runs it in the background, so a stop or an intake decision never waits on the gateway.
  - `openclaw_sessions.task_run_session_keys` lists those sessions.
- **Call sites:**
  - `/tasks/{id}/signal` on a whole-message stop or a `cancel` signal. This is how the plugin routes Kirill's "stop".
  - `/tasks/{id}/cancel`.
  - `temporal_control.terminate_task_workflow`, which intake uses for `rebuild_stale` and supersede.
  - `/dev/suspend-all`.
  - The workflow still sees the stop at its next step, as before. Aura's run is now stopped at once instead of running on to its end.
- `tests/conftest.py`: tests never spawn the real CLI; the abort call is stubbed unless a test drives it.

**Verification:**
- New `tests/test_openclaw_control.py` (5), with a fake `openclaw` executable that logs its arguments, covers:
  - every session aborted with `clearQueued`, with gateway answers, errors and timeouts told apart and the event recorded;
  - a task without sessions spawns nothing;
  - the session list covers Aura and the evaluator;
  - stop, cancel, supersede and rebuild all schedule the abort, and an ordinary attached message does not;
  - a failing background abort is logged, not raised.
- Full suite: 699 passed, 4 skipped.
- **Live, end to end through the module** on a scratch run it found in the live store: the gateway answered `aborted`, and the run ended with stop reason `aborted` 4.5 s after the stop began. Almost all of that is the CLI starting node and connecting. One tool call the model issued at +2.3 s, while the CLI was still starting, still ran; nothing ran after the gateway received the abort.
- A stop of a real long task by Kirill is part of Step 15's acceptance.

**Step 8 addendum, live:** the 15:07 JST canary ran on the new code and completed. Its prompt had 2,308 characters with one memory block (the empty-memory variant, since a canary gets only its own run, which was still empty) and no dialogue. `memory.fast_context` recorded 20 ms and 37 ms.

---

## Step 11 — Deep recall workflow and context report

**Date:** 2026-09-30, deployed 15:53 JST (`2ecf4bf`), follow-up 15:55 JST (`5b0de85`).

**Research** ([LongMemEval, ICLR 2025](https://arxiv.org/abs/2410.10813) and [its code](https://github.com/xiaowu0162/LongMemEval); [Think Big, Search Small, arXiv 2607.07548](https://arxiv.org/html/2607.07548); the Step 1 sources on update chains):
- Time-aware query expansion: an LLM infers a time range from the query and narrows the search. Recall rose 6.8–11.3%, but only with a strong model inferring the range.
- Reading matters even with perfect retrieval. Items presented as JSON and sorted by date, read by extracting notes from each item before reasoning (Chain-of-Note), gained up to 10 points. GPT-4o kept improving past 20k retrieved tokens.
- Turn-level values beat whole sessions, and extracted facts help multi-session questions. This matches our chunks per turn plus facts.
- Decomposing a search into focused sub-queries doubles as context management: the searcher returns a condensed report, not raw passages. This is the IA's report to Aura.

**What changed:**
- `app/deep_memory/recall.py` (new):
  - **Plan** (gpt-6-luna, `recall` priority): whether memory is needed, up to 4 sub-queries with the levels to search, entities, and a time window.
  - **Retrieve:** fused hybrid search per sub-query. The current task's own content is never returned. When the index cannot answer, Postgres text search does.
  - **Expand, within 40 items and 40,000 characters:** a chunk's section summary, its document summary with the table of contents, neighbouring chunks that are still valid, and the tasks linked to its task by lineage (never the current task). A fact brings its supersedes chain, the value that replaced it and the facts that contradict it, each marked current, superseded or conflicting.
  - **Read** (gpt-6-luna): the evidence as numbered JSON items in date order, noted item by item before the report is written. The ContextReport has facts (status, as-of date, citations), past tasks (summary, outcome), document sections, gaps and a brief for Aura. Evidence numbers resolve to refs (`chunk:…`, `fact:…`); numbers the model writes into the brief are stripped.
  - `format_report` gives the block Aura and the evaluator will read in Step 13. An irrelevant report gives nothing.
  - Dates are Kirill's local (Tokyo) day, for index timestamps too. The window covers whole local days: `until` includes its day.
- `app/activities/deep_memory_activities.py` (new): start, plan, retrieve, read and close, each recording its step in `dm_context_reports`: status (running → ready, empty, skipped or failed), plan, candidate refs, report, brief, token use and latency.
- `app/workflows/deep_recall.py` (new): `DeepRecallWorkflow`, id `{task_id}-recall`. Plan, retrieve and read each get two tries. A request that needs no memory closes as skipped, nothing found as empty, and a failed step as failed, so the parent always learns the outcome. Step 13's parent bounds the runtime with an execution timeout.
- `worker.py` registers the workflow and activities.
- **The janitor, the purge tool, the reconciler's orphan cleanup and the stuck-workflow count recognise `-recall`.** A running recall counts as stuck only once its task has ended; the parent bounds it while the task runs.
- **Two fixes from the live run:**
  - `app/llm/openai_direct.py`: the reasoning item no longer counts as the first output. Timed live: the stream opens the reasoning item at +2.2 s, then stays silent for 4.4 s while the model thinks (599 reasoning tokens). A read that needed more thinking passed the 5 s gap, both gpt-6-luna attempts were cut off, and gpt-oss-20b answered after 27 s. The model now has the 20 s first-output budget for thinking, then the 5 s gaps, as the rule states.
  - `app/deep_memory/enrich.py`: a section answer that miscounts its chunk contexts is asked again once. Live, the model returned contexts 0–3 for a 3-chunk guide section. Three reruns of the same call returned 0–2, correctly aligned. Before, one slip discarded every section call of the document and the job redid them all (about 40k tokens for the guide).

**Deviations:**
- The window ranks rather than filters, unlike LongMemEval. Hits inside the window come first and the others still count, since people misdate things ("last week" for ten days ago). The reader sees both.
- The recall modules were drafted before this research was logged. The research then changed the reader (JSON items in date order, note before writing, 24,000 → 40,000 evidence characters) and confirmed the rest.

**Verification:**
- New `tests/test_deep_recall.py` (12), on a fixture index over a real SQLite schema, covers:
  - expansion, with the current task excluded and a retired neighbour skipped;
  - the evidence budget;
  - window ranking with Tokyo day bounds;
  - text-search fallback;
  - plan trimming;
  - cited reading in date order, with untrusted items flagged;
  - the formatted block;
  - the workflow in Temporal's test server (ready, skipped, empty, irrelevant, and a failed read retried once), with the persisted row.
- Other new tests:
  - `tests/test_reconciler_janitor.py` (+3): orphan recall cleanup, the janitor reading a recall's task, the stuck count.
  - `tests/test_openai_direct.py` (+2): thinking within the first-output budget stays on gpt-6-luna; thinking past it falls back. The first failed on the old code.
  - `tests/test_deep_enrich.py` (+1): a miscounted answer is asked again.
- Full suite: 717 passed, 4 skipped.
- **Live over real past tasks**, on production Postgres rolled back, with a scratch Qdrant collection deleted afterwards. The code-word and Kubernetes-guide tasks were ingested and enriched (82 s), and 161 points indexed with the 51 live user facts. Recall ran as a new task, each question once:

  | Question | Plan | Retrieve | Read | Total | Report |
  |---|---|---|---|---|---|
  | "What's my test code word?" | 1.7 s | 0.5 s | 5.4 s | 7.6 s | PELICAN-47, current, cited to the earlier answer |
  | "Which Kubernetes docs did we use for the home cluster guide?" | 2.3 s | 0.4 s | 27.4 s (fallback, fixed above) | 30.1 s | releases page and eight K3s docs |
  | "How many servers did you recommend for etcd, and why?" | 2.1 s | 0.4 s | 7.1 s | 9.6 s | three, quorum of two tolerates one loss |

  Every citation resolved to retrieved evidence.
- **Live on the worker** after the deploy: `{task_id}-recall` workflows for a real task, `trigger=probe`. They ran in 11.7 s and 15.9 s and ended `ready`, with the plan, 8 candidates, token use and latency (10,892 and 14,887 ms) persisted. The first run's brief ended with a bracketed note echoing the new instruction against evidence numbers. The instruction was removed; the strip does that job (`5b0de85`), and the second brief is clean. The two probe rows stay in `dm_context_reports`.
- Readiness after the deploy: 30 pass, 1 warn (telemetry, by design), 0 fail.

**Observations:**
- **The live index holds no documents yet.** There have been no user tasks since go-live, and there is no backfill. The three queued task jobs are hourly canaries, closed as `internal` as designed.
- **Most of the 51 active user-memory rows are legacy noise:** "Site referenced: …" entries from the old promotion heuristic, and fragments of Aura's workspace files (USER.md, the imperatives, heartbeat notes). They show up as evidence, and the reader discards them. Removing them needs Kirill's approval; Step 14's invariants will surface them.
- The healthcheck warns that OpenClaw prompt tokens over 24 h (6.0M) exceed its 5M budget. The total comes from 32 user tasks (with yesterday's reworked Kubernetes guide), 35 canaries and the Step 9–10 scratch runs. The memory lane is separate (142k today).

---
