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

## Step 12 — Intake upgrades

**Date:** 2026-09-30, deployed 16:39 JST (`d14e16b`).

**What changed:**
- **`recall_depth`** (none or deep): whether answering needs a search of long-term memory.
  - Jev: a new typed question in `app/decisions/intake.py`. Its answer counts at Jev's intake confidence bar (0.85) or above; otherwise the value is deep.
  - The intake analyst returns it in its JSON; the prompt says when to choose deep, and deep when unsure.
  - `apply_intake_policy` keeps none only when the decision's confidence reaches the intake threshold (65) and the value is valid. Otherwise it is deep: missing, invalid, degraded, or from a deterministic gate.
  - It is written to the decision audit (`llm_raw`, the `intake.decided` event) and passed to the task workflow as `recall_depth`. Step 13 reads it.
- **The analyst reads the recent dialogue.**
  - `assemble_intake_context` fetches the session's recent turns once, beside retrieval, as `recent_dialogue`. Jev reads the same lines; its own fetch is gone.
  - The analyst's context lists `recent_dialogue` first. It says that finished tasks carry their outcome and Aura's answer, which the registry leads with since Step 6.
  - The analyst's context JSON keeps non-ASCII text as is (`ensure_ascii=False`). Before, Japanese turned into `\u` escapes, several times longer against the 16,000-character cap.
- **Text search** (`search_fts` in `app/task_registry/hybrid_retriever.py`):
  - any word of the message (`websearch_to_tsquery` with `or`), where `plainto_tsquery` needed every word;
  - no canary or heartbeat rows, in SQL and again per row;
  - the last 90 days only.
- **Per-leg deadlines.** Text search (3 s) and user memory (4 s) run concurrently with the active, registry and vector legs, each within its own deadline, capped by the vector deadline. Before, both ran after the others inside one 4 s timeout, so a slow embedder threw away the text hits as well. The context phase now takes at most the vector deadline plus 2 s of liveness checks, instead of up to 14 s.
- `ops/jev_eval.py` validates and scores an optional `recall_depth` label on its own; it is not part of the routing gate. 32 fixture cases are labelled: 17 none, 15 deep.

**Verification:**
- New tests:
  - `tests/test_jev_intake.py` (+11): the question set, Jev's answer and its threshold, the policy's defaults, and Jev reading the assembled dialogue.
  - `tests/test_hybrid_retriever.py` (+2, 1 replaced): the OR query; the SQL's OR, internal filter and time bound; a slow memory leg that keeps the text hits.
  - `tests/test_intake_bounded_context.py` (+1): the dialogue is read once, beside retrieval.
  - `tests/test_intake_evidence.py` (+1): the analyst's prompt, with Japanese kept readable.
  - `tests/test_temporal_connect.py` (+1): `recall_depth` reaches the workflow payload.
- Full suite: 733 passed, 4 skipped.
- One timing test failed once at 0.36 s against a 0.30 s bound, because the new dialogue fetch ran against an empty SQLite file. The test now stubs the fetch, like its other I/O, and passed in three reruns and the full suite. The suite took 425–515 s instead of about 260 s, under measured disk-I/O pressure (`/proc/pressure/io`, about 7% full over 5 minutes). The whole-path harness took 105 s here and 95 s on `main`.
- **Harness** (`ops/jev_eval.py --intake`): all 51 cases validate.
  - Largest fixture request: 5,727 bytes. Worst case the builder allows in ASCII: 21,250 bytes, under the 24,000 limit.
  - The same worst case in Japanese is 49,450 bytes. This predates this step, which adds about 450 bytes. Jev then answers `request_too_large` and the analyst decides.
- **Harness live** against Jev, with the step before as baseline:

  | | Accepted | Accuracy on accepted | Harmful | p95 |
  |---|---|---|---|---|
  | Before (`ab9565a`) | 34 of 51 | 0.882 | 0 | 312 ms |
  | Step 12 | 35 of 51 | 0.886 | 0 | 424 ms |

  - The gate's 0.9 accuracy bar fails before and after. The 4 errors are execution mode and catalog on running-task and login cases, not this step. The other answers moved only slightly, with one case crossing the bar each way.
  - `recall_depth`: 20 of 23 labelled accepted cases right (0.87). Two misses were none answers below 0.85, which became deep. One was a confident none for "Create an account … using my work email", where memory is needed.
- **Live previews** on the worker (`/tasks/intake/preview`: no task, no Slack), Kirill's session:
  - "Which Kubernetes docs did we use for the home cluster guide?" (analyst): create_guided, deep, 19 s.
  - The same question reworded, Jev first: Jev abstained (deep at 0.42); the analyst decided create_guided, deep.
  - "Good morning!": Jev, none at 1.0, 1.7 s.
  - "What's my test code word?": Jev, none at 0.88, 1.9 s. This is right: this session's recent dialogue holds that exact exchange with the answer.
  - The analyst's prompt, read from OpenClaw's agent database, held the 8 recent turns and, among its text hits, the Kubernetes guide task.
  - On live Postgres, that question's registry search found 0 entries with every word required and 12 with any word.
  - No leg timeouts in the logs. Readiness: 30 pass, 1 warn, 0 fail.

**Deviations:**
- The `ensure_ascii=False` change is not in the plan. The analyst could not read Japanese dialogue without it.
- A Japanese conversation at the builder's limits can exceed Jev's 24 KB. This is left to the analyst as designed, not trimmed.

---

## Step 13 — Two-phase answering

**Date:** 2026-09-30, deployed 17:53 JST (`a28492c`). Recall and follow-ups stay off until Step 15.

**What changed:**
- **Starting recall.** When a task's run starts, `start_task_workflow` has already decided whether it recalls (`deep_recall` in the payload): the `recall_enabled` switch is on, intake did not say `recall_depth: none`, and the task is not internal. The workflow checks the internal part again.
  - It starts the `{task_id}-recall` child with a report id it chose. The child's runtime is capped at `recall_deadline_sec` (180 s), and nothing waits for it.
  - The new behaviour is behind one `workflow.patched("deep-memory-two-phase")` marker, so runs started before this deploy never recall.
- **Report ready in time** (`app/workflows/recall_phase.py`, a mixin like the evaluator and attached-message ones). It is checked, without waiting, before each plan step, each judgment and each rework brief.
  - A relevant report joins the one memory block Aura and the evaluator read, and stays in every memory block rebuilt after it.
  - The row records the consumer: `steps` or `evaluator`. No follow-up follows.
- **Report after the reply** (generic tasks, only with `followups_enabled`):
  - The task stays running and refreshes its liveness. It waits up to `followup_wait_sec` (300 s) for the recall, a stop, or a new message.
  - A stop ends the task as stopped.
  - A new message lets the recall go. The task completes, and the message starts over at intake as its own task, since intake would otherwise attach it to the waiting task again.
  - A relevant report goes to the IA novelty judge (`judge_recall_novelty`, gpt-6-luna): none, adds or corrects, with points. Adds or corrects without points count as none.
  - For adds or corrects, RMP sends a notice in the words of your request: "I recalled some more information from our earlier conversations. I need a little time to work it into a more accurate answer, and I'll get back to you."
  - Aura then refines in a fresh `__recall` session. Her brief holds the request, the reply as sent, what memory adds or corrects, the memory block with the report, and the actions digest. She gives the whole answer if her reply was short, or only what changes if it was long.
  - The evaluator judges the refinement as a follow-up: its brief carries the reply as sent. Verdict sessions continue the task's attempt count (`__v<n>`), with up to three judged attempts and reworks in fresh `__r<n>` sessions. Attached messages are folded in as usual.
  - Only an accepted refinement is sent, as kind `followup`, with the verdict's attempt in its metadata. Otherwise Kirill hears "I couldn't confirm the refined answer, so my earlier reply stands."
  - The task completes after the follow-up, so its document and facts include it.
- **Catalog tasks** check for a finished report only at step boundaries (before each step attempt), with no follow-up. The recall is let go when a leg finishes, including before a durable task waits for its next leg.
- **Every exit settles the report row** (`settle_recall_report`): consumed by `steps`, `evaluator`, `followup` or `none`. A report still running when the task lets it go is closed as `cancelled`, and a failed or timed-out recall as `failed`.
- `start_recall_report` takes the task's report id, and a retried start finds the row it wrote.

**Verification:**
- **New `tests/test_two_phase_answering.py`**: 16 scenarios on Temporal's test server, with the recall child replaced by a scripted workflow.
  - A report ready before judgment joins the evaluator's brief, and no follow-up follows.
  - Adds: notice, `__recall` refinement, judged as a follow-up at attempt 2, delivered as `followup`.
  - Corrects.
  - Nothing new.
  - An irrelevant report.
  - A failed recall; a recall past its deadline; a recall slower than the follow-up wait.
  - A stop during the wait; a message during the wait (resubmitted, recall let go).
  - A follow-up never accepted (the stands notice).
  - An internal task; recall off; follow-ups off.
  - Catalog: the report appears from the next step boundary, and a late report is let go.
- **Ordering in the harness.** Timer-based ordering was flaky: the test server skips time whenever all workflows are idle, so a scripted recall could land early. Each scenario now releases the recall on an explicit event (after the reply, at once, or never), and waits until the parent's history holds the child's completion when a report must arrive first. 10 of 10 repeated runs passed afterwards. One earlier failure was my sequencing: a test run started in parallel with the edit it needed.
- **Whole-path scenario** (real API, intake, workflow and database; the recall row written by the real activities):
  - Slack received the reply, the notice and the follow-up.
  - The log reads request, verdict, reply, notice, verdict, follow-up.
  - The report ended `ready`, consumed by the follow-up, with novelty `adds`.
  - Two evaluator accepts; the judged-delivery, attached-message and Slack invariants pass.
- **Replay:** four histories recorded by the code before this step (accept, rework, a message after the reply, a stop while judging) replay on the new code (`tests/test_workflow_replay.py`, fixture committed). A variant with one extra timer fails that replay with a nondeterminism error, so the check does catch it.
- Unit tests: the novelty judge's points rule and reply cap; the report row's id, novelty and settling; the recall settings the workflow receives.
- Full suite: 754 passed, 4 skipped.
- **Live:**
  - The watcher restarted both services at 17:53 JST. Readiness: 30 pass, 1 warn, 0 fail. `recall_enabled` and `followups_enabled` are off.
  - The novelty judge on gpt-6-luna, against the Step 11 etcd report: a reply that lacks it gave adds (2.8 s), one that contradicts it corrects (3.6 s), and one that covers it none (1.9 s). The points carried the value from memory.

**Deviations:**
- Not in the plan: a new message during the wait lets the recall go, instead of feeding the refinement. This keeps Kirill's new message from waiting behind a recall, and avoids intake attaching it to the waiting task again.
- A refinement the evaluator does not accept within three attempts is not escalated like a first answer. The reply already stands, so Kirill hears that it does.
- No live run of a follow-up yet. A scratch user task would be ingested into Kirill's memory, so the first live follow-up is part of Step 15's acceptance with him.

---

## Step 14 — Observability, invariants, docs

**Date:** 2026-09-30, deployed 18:31 JST (`5706091`).

**What changed:**
- `app/deep_memory/health.py` (new):
  - **Readiness checks**, in `run_all_checks`:
    - `deep_memory_ingest`: a due job waiting 15 min warns and 60 min fails; so does a job with 3+ failed attempts.
    - `deep_memory_enrichment`: a document raw for over 30 min, or an enrichment still failing, warns. Budget-deferred jobs are named.
    - `deep_memory_index`: a missing collection fails; points and objects apart by more than the queued outbox warns.
    - `memory_lane`: at 80% of the daily token budget warns, at 100% fails.
    - `deep_recall`: report p95 over 90 s, a quarter of recalls failed, or follow-ups after more than half of the ready reports warn (at least 4 samples). It passes with "Recall off" while the switch is off.
  - **Invariants**, in `invariants.CHECKS` and so paged by the sentinel:
    - `task_documents`: every user task finished since go-live (the first queued ingest job) has an enriched task document 30 minutes after it ended. "User task" follows `ingestible()`, the rule ingestion itself uses.
    - `deep_index_internal`: no live deep-memory document belongs to a task `ingestible()` refuses (canary, heartbeat, cron, internal intent, intake placeholder).
    - `judged_followups`: every `followup` message has an `evaluator.accept` for the same attempt, recorded before it.
  - **Views** `deep_memory_status()` and `deep_memory_reports(task_id)`. Each status part (ingest, index, lane, recall) reports its own error instead of failing the whole view.
- `GET /api/deep_memory/status` and `GET /api/deep_memory/reports/{task_id}`, behind the API key.
- `ops/healthcheck.sh` prints a line per deep-memory check.
- `index.count_points()` (exact count).
- **Docs:**
  - CONCEPT_TREE: the IA as an actor, the deep-memory stores, `DeepRecallWorkflow`, the fast-context and two-phase invariants, turn path step 10, the memory doctrine, the IA's model law, a contradiction-register row for Kirill's direct-call decision, and the pointer map.
  - ARCHITECTURE: flow steps 5, 6 and 9; recall in both workflows; the recall child; readiness and the ingest loop; the endpoints; new §6.3 on deep memory and the IA; the file index.
  - README: the flow and diagram, guarantees, memory layers, Jev's new question, testing and layout.
- **Rule 2**, in `/root/.cursor/rules/rmp-architecture.mdc` and the repository copy, now reads: "…`rmp_intake_*`); the IA's background memory work calls OpenAI directly (gpt-6-luna, medium, store false)." `tests/test_rule_copies.py` compares the copies wherever the host rule exists.
- `tests/test_fast_context.py`: the deadline test gets headroom (0.8 s deadline, 2 s slow leg). It failed once in a full run under measured disk pressure, with the dialogue and run legs over its 0.2 s deadline. It passed 5 of 5 alone before the change and 3 of 3 after.

**Verification:**
- New `tests/test_deep_memory_health.py` (14):
  - each readiness check's pass, warn and fail;
  - the invariants: tasks before go-live, still inside 30 minutes, or internal are ignored; the other attempt and an accept after the follow-up count as failures;
  - the endpoints, and a part that cannot be read.
- The whole-path follow-up scenario now also passes `judged_followups` on its real data.
- Full suite: 769 passed, 4 skipped.
- The rule copies are byte-identical (SHA-256 `530d7643…`), including the `main` checkout after the merge.
- **Live** after the watcher's restart at 18:31 JST:
  - Readiness: 38 pass, 1 warn (telemetry, by design), 0 fail. The healthcheck's new lines: ingest drains (0 pending), documents enriched on time, 51 points for 51 objects, memory lane at 4% of its budget, recall off, 0 finished user tasks to check, no internal content, 0 follow-ups.
  - `/api/deep_memory/status` answered with the switches (recall and follow-ups off), queue, index (51 = 51), lane (151,007 of 4,000,000 tokens) and recall (none in 24 h). `/reports/88035ce7…` returned the two Step 11 probe reports. Without the key: 401.

**Step 13 addendum, live:** the 18:07 JST canary ran on the two-phase code and completed in 13 s, with no recall (internal task).

**Deviation:** the plan gave readiness, invariants and docs to Grok 4.7 extra-high subagents. Kirill's rule names Grok 4.5 High, which is not among the available models, and the Grok 4.7 subagents had stalled on a provider limit earlier today (Step 4). I wrote these modules myself, as in Steps 4–5.

---

## Step 15 — End-to-end proof and close

**Date:** 2026-09-30 (18:40–19:05 JST so far).

**Retrieval eval:**
- `ops/deep_memory_eval.py` and the labelled fixture `tests/fixtures/deep_memory_eval.json`.
  - 50 items: facts, conversation turns, guide sections, fetched pages, task records and incidents, with near-miss distractors (another code word, a neighbouring SKU, other Kubernetes versions).
  - 32 queries, exact and paraphrased.
- Offline it validates the fixture. `--live` indexes the corpus into a throwaway collection, runs each query four ways, and deletes the collection. `tests/test_deep_memory_eval.py` (3) checks the fixture and the metrics.
- Live, with `text-embedding-3-large`, k = 8 (the throwaway collection was deleted after the run):

  | Mode | recall@8 | MRR |
  |---|---|---|
  | Hybrid RRF (what the index uses) | 1.000 | 1.000 |
  | Hybrid DBSF | 1.000 | 0.984 |
  | Dense only | 1.000 | 0.958 |
  | BM25 only | 0.938 | 0.930 |

  - Gate passed: hybrid ≥ 0.9 and not below dense.
  - Recall@8 saturates on a set this size, so the ranking difference shows in MRR: hybrid RRF ranked a relevant item first for every query. RRF stays; DBSF brings nothing on this set.
  - BM25 alone missed "Which timezone am I in?" (no shared words with "Japan Standard Time").
- Before the push, an invented health fact in the fixture was replaced with a neutral preference, and the eval was re-run with the same numbers.

**Deploy:**
- The code is live through the watcher: the last application change was Step 14's, and Step 15 changed only `ops/`, `tests/` and docs.
- The collection, the user-memory migration and the procedural delete were done in Steps 4 and 7. Checked again: 51 user facts = 51 points, and the one remaining procedural row is the real procedure summary Step 7 kept.
- **Recall and follow-ups on** at 18:54 JST: `deep_memory.recall_enabled` and `followups_enabled` set through the locked settings writer. The API's status view shows both on; the next user task recalls.

**Proof:**
- Full suite: 772 passed, 4 skipped. It includes the whole-path harness (14 scenarios, the two-phase follow-up among them), the judgment harness (11), the two-phase harness (16) and the replay of recorded histories.
- **Pushed** to `Hyper-AI-Lab/openclaw-jev`: `371c87d..216d31e`.
  - The first CI run failed at collection: the rule-copy test stat-ed `/root/.cursor/rules/…`, which the runner cannot read. The test now skips when the host rule cannot be read.
  - CI run [36698805338](https://github.com/Hyper-AI-Lab/openclaw-jev/actions/runs/36698805338): success, 771 passed, 5 skipped.
- Before the push, the outgoing diff was scanned for credentials. The only match is the fake key the facts tests use to prove credentials are never stored.

**Live acceptance with Kirill** (19:01–19:13 JST, his DMs to Aura, recall and follow-ups on):

| Check | Task | What happened |
|---|---|---|
| A fact, then asked later | `2120d2bd`, `4cadfe37` | "My acceptance code word is KESTREL-58", then, four tasks later, "What's my acceptance code word?": recall ready in 10.0 s, merged before judgment, answered KESTREL-58 in 27 s |
| A long document, then one section | `df3d14e2`, `f7c8b7f1` | Aura read kubernetes.io/releases. Asked later when the 1.34 branch reaches end of life: recall (11.2 s) found the page's section and merged at a step boundary; answered October 27, 2026 |
| A past task's outcome | `384fb4c8` | Reply at +27 s; the report came after it, so the novelty judge ran: adds (3 points), notice, `__recall` refinement, accepted at attempt 2, follow-up at +52 s |
| A rework | `df3d14e2` | Two reworks, accepted at attempt 3. Prompt tokens per attempt: 28,647, then 24,740 and 24,999 (fresh sessions): flat |
| A stop | `cabfadb4` | Kirill heard "stopped as requested" within 3 s and nothing else was sent. But the OpenClaw run was aborted only 52 s later (below) |

Against the targets:
- **Fast path:** fast context 336–1,755 ms over 14 assemblies, so p95 under 3 s. One memory block in each task prompt.
- **Deep path:** reports took 10.0–18.2 s (p95 under 90 s). Intake's `recall_depth` skipped recall for the page read, the NAS guide and the bullets request.
- **Follow-ups:** one, judged (`judged_followups` passes).
- **Reworks:** flat.
- **Regressions:** none. Readiness afterwards: 38 pass, 1 warn, 0 fail. Deep index 139 points = 139 objects; `task_documents` found enriched documents for both tasks past 30 minutes.

**Two findings, two fixes** (`c8ee0e0`, deployed 19:31 JST, CI [36702938542](https://github.com/Hyper-AI-Lab/openclaw-jev/actions/runs/36702938542) green, 774 passed):
- **The follow-up was not worth an interruption.** Its three points were how the work was processed: the evaluator accepted, one Slack message was sent, about a minute. The novelty judge now asks whether Kirill would be misinformed or miss something without the follow-up; processing details and minor background are none. Rechecked live on gpt-6-luna: that case is now none; the etcd cases stay adds, corrects and none.
- **The stop aborted late.** Two CLI calls ran, one for the planner session that had already finished. Each exceeded the 30 s wrapper. The wrapper killed only the CLI's parent, and its node child sent the abort at +52 s, unrecorded. Now finished sessions (gateway store `done`, `failed`, `killed`) are skipped, the CLI gets 120 s in its own process group, and a timeout kills the whole group (tests +3).

**Residual: stop latency on this host.**
- A live re-measure with a scratch run after the fix: the abort reached the gateway at +44.6 s. The run had meanwhile made three tool calls and finished on its own at +16.8 s, so the gateway answered `no-active-run`.
- A trace shows why. The time is in the `openclaw gateway call` CLI before it connects: it re-launches node after 17 s and connects 41 s later (under strace; 7.8 s for an idle `health` call without it). The gateway answers within about 1 s of the connection. At Step 10 this morning the same path took 4.5 s.
- Kirill sees the stop at once and nothing is delivered after it. But a run can go on for up to about 45 s on this host now; for catalog work with side effects, that matters.
- The durable fix is a direct gateway client. The gateway's connection is device-authenticated (the client signs the challenge nonce with a device key pair and applies OpenClaw's token rules), so that touches authentication and needs Kirill's approval. Not done.

---
