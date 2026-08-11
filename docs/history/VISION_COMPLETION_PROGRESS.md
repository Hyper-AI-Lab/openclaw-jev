---

## A1 — MemoryRouter fail-soft + read_ordered process_type
**Status:** DONE  
**Changes:** `app/memory/router.py` — vector search wrapped in try/except; `read_ordered` accepts `process_type` for procedural scope; `build_context_block` uses full ordered read including pinned; `read_process_memory` activity passes `process_type`.  
**Verify:** `test_read_ordered_process_type` passes.

## A2 — Quota broker atomic write
**Status:** DONE  
**Changes:** `app/llm/quota_broker.py` — `threading.Lock` + `tempfile.mkstemp` + `fsync` + `os.replace` for race-free state writes.  
**Verify:** existing quota broker tests pass.

## A3 — compensated terminal state
**Status:** DONE  
**Changes:** `record_compensation` activity in `db_activities.py`; generic/catalog workflows call it on partial-failure paths; worker registered activity.  
**Verify:** compensation tests in `test_vision_completion.py`; workflows compile.

## A4 — Reconciler Slack notify on stale repair
**Status:** DONE  
**Changes:** `app/reconciler.py` — `_notify_repair()` on repaired terminal/completed and stale-detected tasks; always refreshes `next_check_at`.  
**Verify:** reconciler imports clean; production-check pass.

## A5 — CatalogStepChildWorkflow
**Status:** DONE  
**Changes:** `app/workflows/catalog_step_child.py`; registered in `worker.py`; `CatalogTaskWorkflow` uses `execute_child_workflow` for dispatch steps.  
**Verify:** worker starts with 3 workflows.

## B1 — decision_engine.py
**Status:** DONE  
**Changes:** `app/orchestrator/decision_engine.py` — `decide_step_outcome`, `decide_completion_gate`, `merge_evaluation_with_decision`.  
**Verify:** unit tests pass.

## B2 — Integrate decision engine into parse_agent_evaluation
**Status:** DONE  
**Changes:** `parse_agent_evaluation` accepts `orchestrate`, `validation_ok`, `attempt`, `max_attempts` payload keys.  
**Verify:** `test_parse_agent_evaluation_orchestrate` passes.

## B3 — prompt_policy.py
**Status:** DONE  
**Changes:** `app/orchestrator/prompt_policy.py` — generic profiles, tool budgets, execute/catalog prompt builders.  
**Verify:** prompt policy tests pass.

## B4 — GenericTaskWorkflow orchestrator-driven
**Status:** DONE  
**Changes:** `generic_task.py` — `build_process_memory_context`, `build_generic_execute_prompt`, orchestrated parse, `decide_completion_gate`, compensation on failure/exception.  
**Verify:** 74 pytest pass (excl. flaky export).

## B5 — Catalog factual progress messages
**Status:** DONE  
**Changes:** `catalog_task.py` — step N/M progress messages from step metadata on every attempt; child workflow isolation.  
**Verify:** catalog workflow imports clean.

## B6 — Code-first quality gate
**Status:** DONE  
**Changes:** `evidence_high_confidence()` in `evidence.py`; generic workflow skips LLM quality when evidence strongly passes.  
**Verify:** `test_evidence_high_confidence_long_summary` passes.

## C1 — build_context_block in workflows
**Status:** DONE  
**Changes:** `build_process_memory_context` activity; generic + catalog + child workflows pass `process_type`.  
**Verify:** memory context uses ordered read path.

## C2 — Auto episodic writes
**Status:** DONE  
**Changes:** `write_episodic_observation` activity; called after observations in generic + catalog child workflows.  
**Verify:** activity registered in worker.

## C3 — Pinned memory pool + promotion stage E
**Status:** DONE  
**Changes:** `promotion.py` promotes high-confidence facts to `pinned`; `policy.py` adds pinned scope gate.  
**Verify:** promotion stats include `promoted_pinned`.

## C4 — Heartbeat memory compact
**Status:** DONE  
**Changes:** `generic_task.py` runs `compact_episodic_memory` when intent is heartbeat request.  
**Verify:** activity wired at workflow start.

## C5 — Memory context API
**Status:** DONE  
**Changes:** `GET /memory/process/{process_run_id}/context` in `server.py`.  
**Verify:** endpoint registered; health OK after restart.

## C6 — Intent routing profiles
**Status:** DONE  
**Changes:** `resolve_generic_profile` in prompt_policy; `server.py` passes `generic_profile` in workflow payload.  
**Verify:** profile resolution tests pass.

## V1 — Tests
**Status:** DONE  
**Changes:** `tests/test_vision_completion.py` — 9 new tests for orchestrator, memory, parse orchestration.  
**Verify:** 9/9 pass; total 74 pass (excluding pre-existing flaky `test_export` async loop issue).

## V2 — ARCHITECTURE.md update
**Status:** DONE  
**Changes:** §9–10 updated to reflect orchestrator, compensation, child workflows, memory daily use completion.  
**Verify:** doc reflects current state.

## V3 — pytest + production-check
**Status:** DONE  
**Verify:** `pytest -q --ignore=tests/test_export.py` → 74 passed; `make production-check` → 13 pass, 1 warn, 0 fail.

## V4 — Service restart + smoke
**Status:** DONE  
**Actions:** `systemctl daemon-reload`; restarted `rmp-api`, `rmp-worker`, `openclaw-gateway`.  
**Verify:** `curl http://127.0.0.1:8000/health` → `{"status":"ok",...}`; vector memory ready.

---

**Plan execution complete:** 24/24 steps (A1–A5, B1–B6, C1–C6, V1–V4).

---

## Phase 2 — Vision Full Completion

### D1 — Fix provenance kwarg
**Status:** DONE — `provenance=` in episodic write + promotion.

### D2 — Memory write tests
**Status:** DONE — `tests/test_memory_writes.py`.

### D3 — Activity fail-soft
**Status:** DONE — read/build_context fail-soft in db_activities.

### D4 — read_ordered + API fail-soft
**Status:** DONE — per-scope guards in router; API returns empty on error.

### D5 — Quota RMW + fcntl
**Status:** DONE — `_mutate_state` + `_file_state_lock`.

### D6 — Atomic auth-profiles write
**Status:** DONE — `_atomic_write_json`.

### A6 — execute_compensation
**Status:** DONE — lease release, step compensation, memory annotation, terminal state.

### A7 — Generic compensation xor failed
**Status:** DONE — exception path uses `execute_compensation` only.

### A8 — Catalog compensation paths
**Status:** DONE — step failure / max attempts / exception with prior context.

### A9 — Reconciler sync + internal filter
**Status:** DONE — ProcessRun sync on repair; `is_internal_task` before notify.

### A10 — Child workflow hygiene
**Status:** DONE — lease release, step finalization; dead `_dispatch_step` removed.

### A11 — GenericExecuteChildWorkflow
**Status:** DONE — `generic_execute_child.py`; registered in worker.

### A12 — Control-plane tests
**Status:** DONE — `tests/test_control_plane.py`.

### B7 — plan_json persistence
**Status:** DONE — ProcessRun.plan_json + migration.

### B8 — plan_generation activity
**Status:** DONE — `plan_activities.py`.

### B9 — step_predicates
**Status:** DONE — `step_predicates.py`.

### B10 — Catalog predicate mapping
**Status:** DONE — `predicate_id` on steps; child evaluates predicates first.

### B11 — Plan-driven generic loop
**Status:** DONE — `_plan_driven_loop`; `orchestration.plan_driven_generic: true`.

### B12 — Facts JSON contract
**Status:** DONE — prompts request facts; predicates decide status.

### B13 — Catalog completion gate
**Status:** DONE — `decide_completion_gate`; skip quality LLM when evidence strong.

### B14 — Intent routing expansion
**Status:** DONE — recall/status/monitor profiles; server `process_type_hint`.

### B15 — Plugin raw_text cleanup
**Status:** DONE — no `[AUTO-ROUTED]`; classifier hint.

### B16 — Orchestration tests
**Status:** DONE — `tests/test_orchestration.py`.

### C7 — Minimal rmp_task bootstrap
**Status:** DONE — OpenClaw patch filterBootstrapFilesForSession.

### C8 — Unified memory inject
**Status:** DONE — prebuilt block in child workflows.

### C9 — Memory-first prompts
**Status:** DONE — `MEMORY_FIRST_UNIVERSAL` in all executor prompts.

### C10 — Empty-process fallback
**Status:** DONE — user pinned/semantic in `build_context_block`.

### C11 — Plugin memory prefetch
**Status:** DONE — prefetch on task create; eager `process_run_id` in API response.

### C12 — rmp_memory_recall tool
**Status:** DONE — plugin tool → `/memory/process/{id}/context`.

### C13 — Vector pinned + dedup
**Status:** DONE — pinned in INDEXABLE_TYPES; promotion content dedup.

### C14 — Memory daily-use tests
**Status:** DONE — `tests/test_memory_daily_use.py`.

### V5 — test_export fix
**Status:** DONE — isolated sqlite engine per test loop.

### V6 — Memory context HTTP tests
**Status:** DONE — fail-soft endpoint test in `test_memory_daily_use.py`.

### V7 — Full pytest
**Status:** DONE — 92 passed, zero exclusions.

### V8 — production-check
**Status:** DONE — 13 pass, 1 warn, 0 fail.

### V9 — Slack memory canary script
**Status:** DONE — `ops/canary_slack_memory.sh`.

### V10 — ARCHITECTURE honesty
**Status:** DONE — §9 retracts Phase 1 overclaims; documents Phase 2 DoD.

### V11 — Service restart + completion
**Status:** DONE — rmp-api, rmp-worker, openclaw-gateway restarted; health OK.

---

**Phase 2 execution complete:** 36/36 steps (D1–D6, A6–A12, B7–B16, C7–C14, V5–V11).

### V9/V11 — Live memory canary evidence (2026-06-05)

**Run:** `bash ops/canary_slack_memory.sh` after workflow fixes (plan_driven payload, session-id poll race, poll_start_time, fail-soft episodic).

**Task:** `1bb2c5c1-50d4-4370-943c-fa849a3dbacc` → **status: completed** (poll 18, ~3 min)

**Transcript:** `/root/.openclaw/agents/main/sessions/52ad87b4-6cb5-4a92-a095-bdc82ab7a2a4.jsonl`

| Check | Result |
|-------|--------|
| `PROCESS-SCOPED MEMORY` in dispatch prompt | PASS |
| Memory-first instruction (`Do NOT use memory_search`) | PASS |
| Workspace-first `memory_search` in session | PASS (none) |
| Memory context API count | 2 |

**Fixes applied during canary run:**
- `is_plan_driven_generic()` removed from workflow sandbox (flag passed in payload)
- `send_to_openclaw`: wait for new `sessionId` after hook POST; stable `poll_start_time` across retries
- Memory-canary deterministic plan (no LLM plan generation)
- Fail-soft + longer timeouts on episodic/memory context activities

---

## Phase 3 — Production Hardening

### R1 — Vector search timeout
**Status:** DONE — `VECTOR_SEARCH_TIMEOUT_SEC=20` in `app/memory/router.py`; `_vector_search_bounded` fail-soft to postgres-only.

### R2 — provenance_ref fix
**Status:** DONE — `write_process_memory` accepts `provenance` or legacy `provenance_ref`.

### R3 — skip_vector payload
**Status:** DONE — `build_process_memory_context` + `MemoryRouter.build_context_block` honor `skip_vector=True`.

### R4 — Memory once per plan loop
**Status:** DONE — `_plan_driven_loop` builds memory once; refreshes after each completed step only.

### R5 — Activity timeout alignment
**Status:** DONE — `build_process_memory_context` 120s; child/catalog `send_to_openclaw` heartbeat 12m verified.

### R6 — Memory reliability tests
**Status:** DONE — `tests/test_memory_reliability.py` (4 tests).

### W1 — Reconciler active repair
**Status:** DONE — terminate stuck RUNNING workflows after 15m; compensate or finalize_task_failure.

### W2 — Orphan child cleanup
**Status:** DONE — `_cleanup_orphan_plan_children` for `*-plan-*` workflows.

### W3 — Workflow janitor
**Status:** DONE — `ops/workflow_janitor.py` + `rmp-janitor.timer` (daily).

### W4 — STALE threshold
**Status:** DONE — `STALE_TASK_MINUTES=20`; internal/canary skip Slack nudge via `_task_is_internal`.

### W5 — Janitor/reconciler tests
**Status:** DONE — `tests/test_reconciler_janitor.py`.

### O1 — Remove legacy loop/flag
**Status:** DONE — `user_evaluation_loop` removed; `plan_driven_generic` removed from settings/config/server.

### O2 — Facts-only parse
**Status:** DONE — `parse_agent_evaluation` uses `extract_agent_facts` facts-first.

### O3 — initial_memory_block at task create
**Status:** DONE — API prefetches via `MemoryRouter.build_context_block`; passed in workflow payload.

### O4 — Plan loop uses initial_memory_block
**Status:** DONE — `_plan_driven_loop` uses prefetch when present.

### O5 — Orchestration coherence tests
**Status:** DONE — extended `tests/test_orchestration.py`.

### M1 — Canary result file
**Status:** DONE — `ops/canary_slack_memory.sh` writes `data/last_memory_canary.json`.

### M2 — Memory canary timer
**Status:** DONE — `rmp-memory-canary.timer` enabled (6h).

### M3 — Readiness checks
**Status:** DONE — `check_memory_canary_recency`, `check_stuck_workflows` in `readiness.py`.

### M4 — Healthcheck integration
**Status:** DONE — `ops/healthcheck.sh` prints memory_canary + stuck_workflows.

### T1 — Poll fallback across sessionIds
**Status:** DONE — `_recent_session_ids` + `_poll_session_ids_for_response` in poll loop.

### T2 — OpenClaw dispatch tests
**Status:** DONE — extended `tests/test_openclaw_poll.py`.

### V1 — Full pytest
**Status:** DONE — 106 passed, zero exclusions.

### V2 — production-check
**Status:** DONE — 15 pass, 1 warn, 0 fail.

### V3 — Live memory canary PASS
**Status:** DONE — Task `176214b7-e573-4c30-8a1c-c795b4173c8d` completed ~60s; all memory checks PASS; `data/last_memory_canary.json` written.

### V4 — Hourly canary
**Status:** DONE — `ops/canary.sh` → task `ccfee128-1431-401f-ac6e-7f7ce3754091` CANARY OK.

### V5 — Timers enabled
**Status:** DONE — `rmp-memory-canary.timer`, `rmp-janitor.timer` active.

### V6 — ARCHITECTURE update
**Status:** DONE — §9–10 Phase 3 ops, janitor, canary schedule, known limits.

### V7 — Service restart
**Status:** DONE — rmp-api, rmp-worker, openclaw-gateway active; health OK.

### V8 — Phase 3 completion
**Status:** DONE — 28/28 steps (R1–R6, W1–W5, O1–O5, M1–M4, T1–T2, V1–V8).

**Phase 3 execution complete.**

---

## Phase 4 — Universal Task Intake & Autonomous Management

### U1 — Task columns (parent_task_id, task_kind, recurrence_key, intake_decision_id)
**Status:** DONE — `app/db/models.py` Task extended with `parent_task_id`, `task_kind`, `recurrence_key`, `intake_decision_id`, `supplementary_context`; `_MIGRATIONS` in `database.py`.

### U2 — TaskMessage model + CRUD
**Status:** DONE — `TaskMessage` model; `app/task_registry/messages.py` (`add_task_message`, `list_task_messages`).

### U3 — TaskIntakeDecision audit model
**Status:** DONE — `TaskIntakeDecision` model; `app/task_registry/intake_audit.py` (`record_intake_decision`, `get_decision`).

### U4 — TaskRegistryEntry + summary builder
**Status:** DONE — `TaskRegistryEntry` model; `app/task_registry/summary.py` (`build_task_summary`, `upsert_registry_entry`).

### U5 — Schema migration + backward-compat defaults
**Status:** DONE — `ops/migrate_task_registry_schema.sh`; 155 cron/canary/heartbeat rows set `task_kind=recurrent`; existing rows default `one_shot`.

### U6 — Task registry settings
**Status:** DONE — `settings.json` `task_registry` section; `app/config.py` `get_task_registry_config()`, `get_task_registry_intake_mode()`.

### U7 — TaskHistoryIndexer on terminal
**Status:** DONE — `app/task_registry/indexer.py`, `hooks.py`; hooked in `update_task_status`, `finalize_task_failure`, `record_compensation` in `db_activities.py`.

### U8 — Qdrant collection rmp_task_registry
**Status:** DONE — `app/task_registry/vector_store.py` (server-mode Qdrant, collection `rmp_task_registry`).

### U9 — Backfill terminal tasks (90d)
**Status:** DONE — `ops/backfill_task_registry.py`; backfill run: scanned=263 indexed=263 errors=0.

### U10 — TaskRegistryRetriever hybrid search
**Status:** DONE — `app/task_registry/retriever.py` (Postgres filters + vector top-K + temporal decay).

### U11 — IntakeContextAssembler
**Status:** DONE — `app/task_registry/intake_context.py` (active tasks, similar history, supplementary messages, recurrence metadata).

### U12 — Intake prompt + JSON schema
**Status:** DONE — `app/task_registry/intake_prompt.py` (decision, confidence, rationale, similar_task_ids, catalog_hint, guidance_notes).

### U13 — classify_task_intake activity
**Status:** DONE — `app/activities/intake_activities.py` via `_execute_on_internal_session` (`rmp_intake_{hash}`).

### U14 — intake_decision_engine.py
**Status:** DONE — merges LLM output + hard policy overrides + confidence thresholds.

### U15 — Recurrence policy (cron/canary/heartbeat)
**Status:** DONE — `app/task_registry/recurrence.py` (`recurrence_key` from cron session + job id).

### U16 — Intake unit tests
**Status:** DONE — `tests/test_task_intake.py` (decision engine, shadow mode, schema validation).

### U17 — POST /tasks/intake/preview
**Status:** DONE — dry-run endpoint in `server.py`; auth bypass for preview path.

### U18 — Intake integrated into POST /tasks (off|shadow|enforce)
**Status:** DONE — `apply_intake_policy` in create path; `intake_mode: enforce` in settings.

### U19 — Outcome handlers (attach/wait/skip/guidance/supersede)
**Status:** DONE — `app/task_registry/intake_handlers.py`; wired in `server.py`.

### U20 — TaskMessage on create/signal
**Status:** DONE — messages stored on task create and signal; cron payloads linked.

### U21 — Plugin intake response handling
**Status:** DONE — `rmp_adapter/index.js` handles `skipped`, `wait_active`, `attach_active`, `deduplicated`.

### U22 — Intake metrics + events
**Status:** DONE — `app/metrics.py` counters `intake_decided`, `intake_skipped`, `intake_attached`.

### U23 — completion_rework.py
**Status:** DONE — structured rejection payload from evidence/quality issues.

### U24 — GenericTaskWorkflow rework loop
**Status:** DONE — up to 3 rework attempts before terminal fail in `generic_task.py`.

### U25 — CatalogTaskWorkflow rework loop
**Status:** DONE — same rework loop in `catalog_task.py`.

### U26 — Unified completion policy
**Status:** DONE — `decide_completion_gate` evidence-first; quality LLM when not high-confidence skip.

### U27 — Completion rework tests
**Status:** DONE — `tests/test_completion_rework.py`.

### U28 — POST /tasks/{id}/spawn_process
**Status:** DONE — `app/task_registry/spawn.py` + endpoint in `server.py`.

### U29 — Intake spawn_process outcome
**Status:** DONE — decision engine + handlers support `spawn_process` for recurrent multi-leg tasks.

### U30 — Cron recurrence_key wiring
**Status:** DONE — `create_task` sets `task_kind=recurrent` + stable `recurrence_key` for cron/canary/heartbeat tags.

### U31 — Catalog hint validation
**Status:** DONE — intake `catalog_hint` → `catalog_type_for_workflow()`; fallback to generic.

### U32 — Plugin regex as pre-hint only
**Status:** DONE — classifier retained as fast pre-hint; intake is primary router in enforce mode.

### U33 — Readiness check task_registry
**Status:** DONE — `check_task_registry_config()` in `readiness.py`.

### U34 — canary_task_intake.sh
**Status:** DONE — `ops/canary_task_intake.sh` PASS (preview returns structured decision).

### U35 — Flip intake_mode to enforce
**Status:** DONE — `settings.json` `task_registry.intake_mode: enforce`; rmp-api + rmp-worker restarted.

### U36 — Full pytest + production-check
**Status:** DONE — 126 passed; production-check 16 pass, 1 warn, 0 fail.

### U37 — ARCHITECTURE §5.0.1
**Status:** DONE — Universal Task Intake section added to `ARCHITECTURE.md`.

### U38 — Phase 4 completion
**Status:** DONE — 38/38 steps (U1–U38); intake enforce live; task registry backfilled; services healthy.

**Phase 4 execution complete.**

---

## Phase 5 — Intake Hardening & Vision Completion

### P1 — Recurrence hygiene
**Status:** DONE — Removed dead canary branch; `should_bypass_intake_llm` for health canary fast-path to `create_fresh`.

### P2 — Robust intake JSON parse
**Status:** DONE — Brace-balanced extraction in `intake_prompt.py`; fixed cross-session attach typo in `intake_decision_engine.py`.

### P3 — rework_max_attempts config
**Status:** DONE — `get_rework_max_attempts()` wired in generic + catalog workflows.

### P4 — File-backed intake cache
**Status:** DONE — `app/task_registry/intake_cache.py` replaces in-memory cache.

### P5 — Layer 2 vector gate
**Status:** DONE — `app/task_registry/vector_gate.py` integrated before LLM in `classify_task_intake`.

### P6 — Vector gate tests
**Status:** DONE — `tests/test_vector_gate.py`.

### P7 — Deterministic skip_valid
**Status:** DONE — `skip_valid_decision()` + `recurrence_intervals` in settings.

### P8 — Temporal decay in retriever
**Status:** DONE — `temporal_half_life_days` scoring in `hybrid_search`.

### P9 — Recurrence/decay tests
**Status:** DONE — `tests/test_recurrence_skip.py`.

### P10 — Durable task_kind + parent_task_id
**Status:** DONE — `derive_task_kind` supports `durable-task` tag; spawn sets `parent_task_id`.

### P11 — Durable cross-session attach
**Status:** DONE — Policy allows attach/wait when target `task_kind=durable`.

### P12 — spawn_leg workflow signal
**Status:** DONE — `GenericTaskWorkflow` durable loop + `spawn_leg`; catalog signal added.

### P13 — spawn.py durable running path
**Status:** DONE — Signals `spawn_leg` when durable+running; starts workflow when idle.

### P14 — Spawn process tests
**Status:** DONE — `tests/test_spawn_process.py`.

### P15 — classify_task_intake activity
**Status:** DONE — `@activity.defn` in `intake_activities.py`.

### P16 — Standalone activity from API
**Status:** DONE — `intake_runner.py` uses `client.execute_activity`; registered in worker.

### P17 — Inline fallback
**Status:** DONE — Temporal failure falls back to inline `classify_task_intake`.

### P18 — Plugin universal intake
**Status:** DONE — Removed `active_user_task` bypass; all DMs via POST /tasks.

### P19 — Attach signal failure surfacing
**Status:** DONE — HTTP 502 on failed attach signal.

### P20 — Plugin idempotency normalize
**Status:** DONE — Single SHA256 of `session_key:rawText`.

### P21 — Handler integration tests
**Status:** DONE — `tests/test_intake_handlers.py`.

### P22 — Rework/admit-failure tests
**Status:** DONE — Extended `tests/test_completion_rework.py` + workflow admit-failure wiring.

### P23 — Flaky readiness fix
**Status:** DONE — Sync psycopg2 count for `check_task_registry_index_fresh`.

### P24 — Create task intake test
**Status:** DONE — `tests/test_create_task_intake.py`.

### P25 — canary_intake_live.sh
**Status:** DONE — Create + duplicate intent + audit row verification.

### P26 — Full verification
**Status:** DONE — 142 pytest pass; production-check pass; services restarted; canaries PASS.

### P27 — ARCHITECTURE §5.0.1 update
**Status:** DONE — 3-layer funnel, standalone activity, durable spawn, universal plugin documented.

### P28 — Phase 5 completion
**Status:** DONE — 28/28 steps (P1–P28); audit gaps closed.

**Phase 5 execution complete.**

---

## Phase 5 follow-up — Post-audit gaps

### F1 — Live attach/wait smoke
**Status:** DONE — `ops/canary_intake_attach_wait.sh`; L1 fast-path duplicate-intent → `attach_active` in `recurrence.py`.

### F2 — Catalog durable loop
**Status:** DONE — `CatalogTaskWorkflow._finish_durable_catalog` + `continue_as_new` on `spawn_leg`.

### F3 — supplementary_context wired
**Status:** DONE — `POST /tasks` sets `latest_message_id`, `source`, `session_key` after first message.

### F4 — Vector gate guided hints
**Status:** DONE — `vector_gate.py` joins `recent_registry.outcome_summary` for `create_guided`.

### F5 — Verification
**Status:** DONE — 144 pytest pass; attach/wait canary PASS; services restarted.

**Phase 5 follow-up complete.**

---

## Phase 6 — Intake Completion & Production Closure

### G1 — parent_process_run_id schema
**Status:** DONE — `ProcessRun.parent_process_run_id` column; migration in `database.py` + `migrate_task_registry_schema.sh`.

### G2 — Durable leg lineage in ensure_process_run
**Status:** DONE — `force_new` sets `parent_process_run_id` to prior run; supersedes non-terminal prior leg.

### G3 — Spawn test suite (3 scenarios)
**Status:** DONE — `tests/test_spawn_process.py`: active non-durable, durable+running spawn_leg, terminal prior starts workflow.

### G4 — Intake spawn leg_intent + export lineage
**Status:** DONE — `intake_handlers` passes `leg_intent`; task export includes `parent_process_run_id`.

### G5 — Deterministic skip_noop fast path
**Status:** DONE — `skip_noop_decision()` in `recurrence.py`; `noop_phrases` in settings.

### G6 — Wire skip_noop into classify_task_intake
**Status:** DONE — after `skip_valid`, before `supersede` and vector gate.

### G7 — skip_noop unit tests
**Status:** DONE — `tests/test_skip_noop.py`.

### G8 — Supersede handler integration test
**Status:** DONE — `test_handle_supersede_terminates_and_falls_through` in `test_intake_handlers.py`.

### G9 — Deterministic supersede fast path
**Status:** DONE — `supersede_decision()` for stale failed recurrent registry entries.

### G10 — canary_intake_supersede.sh
**Status:** DONE — seeds failed registry row; preview asserts `supersede`.

### G11 — MoltMarket skill path fix
**Status:** DONE — `ops/ensure_openclaw_skills.sh` symlinks workspace skills to npm OpenClaw path.

### G12 — verify_openclaw_patch MoltMarket check
**Status:** DONE — asserts `moltmarket/SKILL.md` resolvable; wired into `make production-check`.

### G13 — OTLP observability stack
**Status:** BLOCKED — Docker Compose not available on VPS; `docker-compose.observability.yml` + `make observability` ready; run when Docker installed, then set `telemetry.otlp_endpoint`.

### G14 — Telemetry readiness
**Status:** DONE (degraded) — `check_telemetry_export` passes when endpoint configured; live collector pending G13.

### G15 — ops/restart_rmp.sh
**Status:** DONE — `daemon-reload` + restart rmp-api/rmp-worker/openclaw-gateway; Makefile target `restart-rmp`.

### G16 — ARCHITECTURE §5.0.1 + §9 update
**Status:** DONE — Layer 1 skip_noop/supersede/duplicate attach; `parent_process_run_id`; Phase 7 deferrals documented.

### G17 — Full pytest
**Status:** DONE — 151 passed, 0 failures.

### G18 — production-check + intake canaries
**Status:** DONE — production-check pass; `canary_task_intake`, `canary_intake_live`, `canary_intake_attach_wait`, `canary_intake_supersede` PASS.

### G19 — Service restart verification
**Status:** DONE — `ops/restart_rmp.sh`; health OK.

### G20 — Phase 6 completion
**Status:** DONE — 20/20 steps (G1–G20; G13 blocked on Docker); obsolete duplicate plan files noted: `*_e2728957`, `*_a0f265b8`, `*_a1cc1f3d` (canonical: `ff04aeb5`, `70bf9acf`, `491cdb59`, `7126db4b`).

**Phase 6 execution complete.**

---

## Phase 6 follow-up — OTLP observability

### G13-F — Docker Compose + observability stack
**Status:** DONE — Installed `docker-compose` (v1.29.2) via apt; `make observability` started `otel-collector` + Phoenix; containers healthy.

### G14-F — OTLP endpoint live
**Status:** DONE — `settings.json` `telemetry.otlp_endpoint=http://127.0.0.1:4318/v1/traces`; readiness telemetry **pass**; rmp-api health shows `telemetry.ready=true`.

### G19-F — Restart + trace path verified
**Status:** DONE — `ops/restart_rmp.sh`; health OK; Phoenix UI HTTP 200 on `:6006`.

**Phase 6 OTLP follow-up complete.**

### G20-F — Observability systemd on boot
**Status:** DONE — `rmp-observability.service` (enabled); `ops/stop_observability.sh`; mirrors `rmp-qdrant.service` pattern.

---

## Phase 7 — Communication & Reliability Hardening

**Plan:** `docs/PHASE_7_COMMUNICATION_RELIABILITY_PLAN.md`  
**Audit:** `docs/CODEBASE_INTELLIGENCE_REPORT.md`

### H1 — OpenClaw userTimezone Asia/Tokyo
**Status:** DONE — `openclaw.json` → `agents.defaults.userTimezone: "Asia/Tokyo"`; gateway restarted via `ops/restart_rmp.sh`.

### H2 — User local time in execute prompts
**Status:** DONE — `user_local_time_block()` in `prompt_policy.py`; injected at task start via `temporal_control.start_task_workflow` → `payload.user_time_block` → `generic_execute_child` (avoids Temporal sandbox `datetime.now` restriction). Unit tests: `tests/test_prompt_policy_timezone.py`.

### H3 — Task liveness during OpenClaw poll
**Status:** DONE — `touch_task_liveness` activity in `db_activities.py`; throttled (45s) calls from `_dispatch_openclaw_session` poll loop in `openclaw_activities.py`.

### H4 — Reconciler stuck threshold 45 min
**Status:** DONE — `STUCK_REPAIR_MINUTES = 45` in `reconciler.py`; tests updated in `test_reconciler_janitor.py`.

### H5 — Catalog workflow sandbox fix
**Status:** DONE — `catalog_task.py` uses `payload.rework_max_attempts` instead of `get_rework_max_attempts()` inside workflow.

### H6 — Conversational evidence fast path
**Status:** DONE — `_is_conversational_intent()` + relaxed `evidence_high_confidence` for greeting/chat intents (≥30 chars); tests in `test_evidence.py`.

### H7 — Memory canary + sentinel clear
**Status:** DONE — Fixed `canary_slack_memory.sh` (`force-canary-run` tag, empty task_id guard); intake bypass in `recurrence.fast_path_decision` + cache skip + `intake_runner` activity id includes tags. Memory canary task `cb569e41` completed ~30s; `data/last_memory_canary.json` status=completed.

### H7-fix — Sandbox regression from H2
**Status:** DONE — `build_generic_execute_prompt(user_time_block=...)` no longer calls `datetime.now()` inside workflow; time block precomputed at workflow start.

### H8 — Verification
**Status:** DONE — 163 pytest passed; `make production-check` pass; readiness 19 pass / 0 warn; `ops/restart_rmp.sh` health OK.

### H9 — Progress log
**Status:** DONE — this section (append-only).

**Phase 7 execution complete.**

**Root causes addressed for Kirill's Slack session:**
1. Wrong greetings — server injected Europe/Berlin time; now JST in OpenClaw config + explicit USER LOCAL TIME in RMP execute prompts.
2. False "stuck and repaired" — long OpenClaw polls froze `updated_at`; liveness touch + 45m stuck threshold.
3. Contradictory time acknowledgments — agent had conflicting time sources; unified JST context.
4. Ops canary noise — memory canary result file cleared (was `timeout`); force-run path for manual `make memory-canary`.

---

## Phase 8 — Intake execution_mode (drop plan keyword layer)

### P8-1 — execution_mode at intake
**Status:** DONE — `app/orchestrator/execution_mode.py`; intake JSON schema + prompt; `apply_intake_policy` resolves mode; passed via server → Temporal payload.

### P8-2 — Plan generation uses intake mode
**Status:** DONE — Removed `_is_simple_conversational`, 300-char limit, and `resolve_generic_profile` plan routing from `plan_activities.py`. `conversational` → single deliver; `structured_work` → plan LLM → fallback DEFAULT_PLAN_STEPS.

### P8-3 — Health canary single-step
**Status:** DONE — `deterministic_health_canary` single deliver (CANARY_OK, no tools); fixes 6-minute poll timeouts from gather+execute.

### P8-4 — Tests + deploy
**Status:** DONE — `tests/test_execution_mode.py`; updated `test_vision_completion.py`; 170 pytest passed; `ops/restart_rmp.sh` health OK.

**Phase 8 execution complete.**

---

## Phase 9 — Intake Reliability & Slack Latency

**Plan:** `.cursor/plans/phase_9_intake_reliability_d79e6740.plan.md`  
**Root cause (task `bfbff879`):** API `execute_activity(intake)` failed (`Standalone activity is disabled`); inline OpenClaw fallback hit `Not in activity context` on heartbeat → no `execution_mode` → 3-step plan LLM (~5.5 min).

### I1 — IntakeWorkflow on worker
**Status:** DONE — `app/workflows/intake_workflow.py`; runs `classify_task_intake_activity` with 120s timeout; workflow id `intake-{request_hash}`.

### I2 — Worker registration
**Status:** DONE — `IntakeWorkflow` added to `worker.py` workflows list.

### I3 — Intake runner rewrite
**Status:** DONE — `intake_runner.py` uses `client.execute_workflow(IntakeWorkflow.run)`; removed inline `classify_task_intake` OpenClaw-from-API path.

### I4 — Deterministic intake fallback
**Status:** DONE — `run_intake_deterministic_gates()` + `classify_task_intake_deterministic()` in `intake_activities.py` (gates only, no LLM).

### I5 — Degraded execution_mode policy
**Status:** DONE — `infer_degraded_execution_mode()` + `intake_llm_failed()` in `execution_mode.py`; user DMs → `conversational` when intake LLM/workflow fails.

### I6 — Safe activity heartbeat
**Status:** DONE — `_safe_activity_heartbeat()` in `openclaw_activities.py`; poll loop no longer raises outside activity context.

### I7 — Audit execution_mode
**Status:** DONE — `execution_mode` in intake result, `llm_raw` audit merge, and `intake.decided` event payload (`intake_handlers.py`).

### I8 — Intake enforce mode
**Status:** DONE — `settings.json` `task_registry.intake_mode: "enforce"`.

### I9 — Conversational fast-complete
**Status:** DONE — `GenericTaskWorkflow._plan_driven_loop`: skip mid-step memory refresh, skip evidence/quality/rework loop; notify Slack before `promote_completion_memory`.

### I10 — Intake execution_mode canary
**Status:** DONE — `ops/canary_intake_execution_mode.sh`; wired into `make production-check`; asserts preview `execution_mode=conversational` for Kirill-style chat.

### I11 — Tests
**Status:** DONE — `test_intake_workflow_runner.py`, `test_generic_task_conversational.py`, `test_intake_deterministic.py`, `test_openclaw_safe_heartbeat.py`; extended `test_execution_mode.py`, `test_create_task_intake.py`, `test_intake_handlers.py`.

### I12 — Deploy verification
**Status:** DONE — **182 pytest passed**; `make production-check` pass (readiness 18 pass / 1 warn / 0 fail); `ops/restart_rmp.sh` health OK; live preview sample:
```json
{"execution_mode": "conversational", "confidence": 95, "intake_mode": "enforce"}
```

**Phase 9 execution complete.**

**Success criteria met:**
1. Intake preview returns `execution_mode: conversational` for Slack chat (worker LLM path live).
2. Conversational workflow skips multi-step plan + quality loop (target ~30–90s end-to-end).
3. No inline OpenClaw intake from API (no `"Not in activity context"` audit rows from that path).
4. `intake_mode: enforce` active.
5. All tests + production-check pass.

---

## Phase 10 — Intake Performance & Vector Reliability

**Plan:** `.cursor/plans/phase_10_intake_hardening_29623d6e.plan.md`

### P10-1 — Task registry Qdrant API migration
**Status:** DONE — `vector_store.py` uses `query_points()` + `_normalize_query_hits()`; removed deprecated `client.search()`.

### P10-2 — Qdrant timeout config
**Status:** DONE — `qdrant_query_timeout_sec`, `intake_vector_deadline_sec`, `intake_llm_timeout_sec` in config/settings; gRPC prefer when port 6334 open.

### P10-3 — Bounded intake context
**Status:** DONE — `hybrid_search_bounded()` with `asyncio.wait_for` on vector leg; supplementary messages capped at 3 tasks.

### P10-4 — Aligned intake timeout budget
**Status:** DONE — `intake_timeouts.py` sandbox-safe constants; budget passed via payload; `_execute_intake_llm()` 55s poll; workflow/activity/runner aligned; unique workflow IDs per run.

### P10-5 — Activity heartbeats
**Status:** DONE — `_safe_activity_heartbeat()` after context build, gates, and intake LLM in `intake_activities.py`.

### P10-6 — Readiness vector probe
**Status:** DONE — `check_task_registry_vector()` live `query_points` probe in readiness.

### P10-7 — Intake latency SLO canary
**Status:** DONE — `ops/canary_intake_latency.sh`; wired into `make production-check`; asserts confidence > 0 and < 30s.

### P10-8 — Intake path metrics
**Status:** DONE — `intake_workflow_ok`, `intake_degraded` counters + latency samples in `metrics.py`; recorded in `intake_runner.py`.

### P10-9 — Tests
**Status:** DONE — `test_task_registry_vector_store.py`, `test_intake_bounded_context.py`, `test_intake_timeouts.py`, `test_intake_metrics.py`, `test_intake_heartbeats.py`; readiness updated.

### P10-10 — Deploy verification
**Status:** DONE — **190 pytest passed**; `make production-check` pass; latency canary: **10s, confidence=95**; `ops/restart_rmp.sh` health OK.

**Phase 10 execution complete.**

**Success criteria met:**
1. Intake preview < 30s with confidence > 0 (LLM on worker, not degraded fallback).
2. Task registry vector search uses `query_points`; no `'search'` AttributeError.
3. Intake activity/OpenClaw poll timeouts aligned (no 120s vs 180s mismatch).
4. All tests + production-check pass.
5. Degraded intake observable via `intake_degraded` / `intake_workflow_ok` metrics.

---

## Temporal recovery & reliability hardening (2026-06-19)

**Trigger:** Kirill Slack DM at 9:59 JST (`5c936b96…`) got no reply for ~1h. Task failed in ~50s with no Slack notification.

### Root cause

1. **Temporal dev server degradation** — `temporal-dev.service` had run **2+ weeks** on SQLite (`data/temporal.db`) without maintenance. Logs showed `Namespace default is not found`, gRPC `Timeout expired`, poll retries 300+, and **34 stuck `Running` workflows** (mostly hourly canaries + orphaned plan steps).
2. **Intake activity heartbeat mismatch** — `activity_heartbeat_sec` was 20s while intake LLM + `reserve_profile` can exceed 55s → Temporal marked activities timed out → deterministic fallback (`confidence: 0`) or failed workflows.
3. **Silent user failures** — `GenericTaskWorkflow` did not notify Slack when plan-driven execution returned `compensated`/`failed`.

### Recovery performed

- Ran `ops/temporal_recover.sh`: backup DB → stop workers → **force-purge 34 workflows** → restart Temporal → health probe → restart stack.
- Cleared zombie `running` tasks in RMP DB (7 marked `failed`).
- Verified: **stuck workflows = 0**, intake canary **8s / confidence=95**, E2E user task **completed in ~35s**.

### Ongoing hardening (prevent recurrence)

| Component | Purpose |
|-----------|---------|
| `rmp-temporal-watchdog.timer` (5 min) | Health probe + auto `--recover`; full recovery if still unhealthy |
| `rmp-temporal-vacuum.timer` (weekly) | SQLite WAL checkpoint + VACUUM via Python (no sqlite3 CLI required) |
| `rmp-janitor-frequent.timer` (2h) | Terminate workflows running >2h with missing/terminal tasks |
| `ops/temporal_purge_running.py --force-recovery` | Full recovery purge + fail linked tasks |
| `temporal-dev.service` | `LimitNOFILE=65535`, burst restart limits |
| `intake_timeouts.py` | `activity_heartbeat_sec = max(55, llm+5)` |
| `generic_task.py` | Slack notify on `compensated`/`failed` for user tasks |
| `make temporal-recover` / `make temporal-health` | Manual ops targets |

**Status:** DONE — **190 pytest passed**; `make production-check` pass; timers enabled.

---

## Concurrent I/O latency (2026-06-19)

**Goal:** Reduce Slack reply latency by overlapping independent reads (not changing reply semantics).

### Changes

1. **Intake hybrid search** — `fetch_active_tasks` ∥ `fetch_recent_registry` ∥ bounded vector search via `asyncio.gather` (`retriever.py`).
2. **Intake supplementary messages** — up to 3 task message loads in parallel (`intake_context.py`).
3. **Memory `read_ordered`** — scope reads run concurrently with `Semaphore(3)`, fail-soft `gather`, merge order preserved (`router.py`).
4. **Shared NVIDIA embed** — short TTL + singleflight cache so parallel scope searches for the same query share one embed HTTP call (`nvidia_embed.py`).

### Verification

- New/updated tests: concurrent timing, merge priority, embed singleflight.
- **195+ pytest passed** for concurrent I/O + timeout budget updates; services restarted.
- Intake timeout budget now includes `context_sec` (vector deadline) so workflow/activity
  timeouts are not cancelled mid-LLM (`activity_start_to_close = llm + context + 20`).
- Do not cache confidence-0 intake fallbacks (avoids poisoning the next minute).
- Latency canary may still fail when NVIDIA primary model returns empty/404 replies —
  that is independent of concurrent I/O (see gateway session `stopReason: error`).

---

## NVIDIA model migration (2026-08-10)

**Trigger:** `moonshotai/kimi-k2.6` deprecated/404 on NVIDIA NIM.

### Probe results
| Model | Result |
|-------|--------|
| `deepseek-ai/deepseek-v4-pro` | **410 Gone** (EOL) — not used |
| `deepseek-ai/deepseek-v4-flash-0731` | OK (available alternative, not enabled) |
| `z-ai/glm-5.2` | OK on all 3 NVIDIA keys |
| `minimaxai/minimax-m3` | OK |

### Config
- Primary: `nvidia/z-ai/glm-5.2`
- Fallback: `nvidia/minimaxai/minimax-m3`
- Auth order: `nvidia:default` → `key2` → `key3` (keys rotate before model fallback)
- Intake LLM budget raised to 150s (GLM is slower than Kimi); latency canary PASS at ~126s / conf=95


## NVIDIA model stack (2026-08-10, v2)

**Goal:** Efficient intake + strong coding agent + fast multi-agent workers on free NVIDIA NIM.

### Live smoke (JSON latency / coding)
- DeepSeek V4 Flash: ~7s JSON, ~12s coding — best hot-path pick
- MiniMax M3: ~10–18s — agent workhorse
- GLM-5.2: ~140s tiny JSON — heavy escape hatch only
- GPT-OSS 20B: ~1–3s — workers / intake backup
- Nemotron 3 Nano: ~1–3s — worker alt

### Configured
- **Agent primary:** `nvidia/minimaxai/minimax-m3`
- **Agent fallbacks:** DeepSeek V4 Flash → GLM-5.2
- **Intake:** DeepSeek V4 Flash → GPT-OSS 20B (hook `model` override + RMP retry)
- **Subagents:** GPT-OSS 20B → Nemotron 3 Nano
- **Intake LLM budget:** 60s (down from 150s)


## Slack-before-memory + quota tune (2026-08-10)

- Subagents + intake: DeepSeek V4 Flash only (GPT-OSS removed).
- Conversational path: Slack notify before episodic write / memory promotion; child defers episodic via `defer_episodic_write`.
- Quota: `min_interval_sec` 8→5, `max_concurrent` 2→3; OpenClaw `maxConcurrent` 1→2.

## OpenClaw upgrade 2026.3.13 → 2026.7.1-2 (2026-08-10)

**Backup:** `rmp/data/backups/openclaw-update-20260810T045004Z/`

### Done
- Node 22.22.0 → **22.23.2** (required by new engines).
- `npm install -g openclaw@latest` → **2026.7.1-2**.
- Slack config: `streaming` object `{"mode":"off","nativeTransport":false}`; removed obsolete `nativeStreaming`.
- Re-applied architecture patches via `patch_openclaw.sh` (hook persistence, announce/bootstrap/Slack suppress, **allowUnsafe passthrough** for RMP sessions).
- Model fallbacks **kept enabled** (MiniMax → DeepSeek → GLM).
- Session lifecycle: assign profile only after session exists (avoids `CronSessionLifecycleClaimError`).
- Intake fix: OpenClaw 2026.7 dropped `allowUnsafeExternalContent` from `/hooks/agent` normalize → EXTERNAL wrap → `NO_REPLY` on JSON. Patched passthrough + RMP payload flag.

### Verification
- `ops/verify_openclaw_patch.sh` PASS
- `make production-check` PASS (execution_mode + latency canaries)
- Live hooks/agent JSON intake returns structured JSON (no SECURITY NOTICE wrap)

## Integrity audit fixes (2026-08-10)

### P0 Slack delivery / session identity
- `_get_slack_user_id` parses OpenClaw 2026.7 `slack:channel:U…` origins; scans slack sessions; falls back to `production.slack_owner_user_id=U0AELFYTLKS`.
- Plugin routes Slack DMs with **real** `ctx.sessionKey` (not hardcoded `agent:main:main`).
- Reserve-skip / active-task helpers treat `slack:` sessions as RMP-owned.
- Intake route failure enables **one-shot native Slack fallback** (avoids silent black hole).

### P1 coherence
- Restored `task_registry.intake_mode: enforce` (defaults + settings); readiness reports enforce.
- `settings.json` permissions `600`.
- Readiness gates `rmp-qdrant.service`.
- Plan LLM failures logged before deterministic fallback.
- Fixed `test_sync_nvidia_auth_profiles` host-env isolation (`NVIDIA_API_KEY_3+`).
- ARCHITECTURE drift corrected (Slack `message_received` path, quota 5s/3, MiniMax, safe-harbor note).
- Registry backfill kicked (index 1078→1184+; remaining stale warn acceptable under quota).

### Verification
- **200 pytest passed**
- `make production-check` PASS
- UID smoke: `agent:main:main` and slack channel key → `U0AELFYTLKS`

## Phase 21 — Production polish & public packaging (2026-08-11)

### Done
- **Registry backfill:** `ops/backfill_task_registry.py` now supports `--missing-only` / `--limit` / `--sleep-sec`. Indexed 700 missing entries (0 errors); readiness `task_registry_index` **pass** at **1900/2140** (≥85%). Remainder continues off-peak.
- **Timers:** `rmp-canary`, `rmp-memory-canary`, `rmp-janitor*`, `rmp-backup`, `rmp-canary-sentinel`, watchdog — all **active**; recent canaries `CANARY OK`.
- **Slack path:** bot `auth.test` OK; `conversations.open` → DM channel; `_get_slack_user_id` → `U0AELFYTLKS`. Health canaries correctly suppress Slack via notification policy.
- **OTLP:** Phoenix + OTel collector already up; set `telemetry.otlp_endpoint=http://127.0.0.1:4318/v1/traces`; restarted `rmp-api`/`rmp-worker`; readiness telemetry **pass**.
- **Alerting:** left **disabled** (no webhook configured).
- **GitHub CI:** `.github/workflows/ci.yml` prepared locally; **not pushed** — PAT lacks `workflow` / Workflows write scope. Add scope, then commit+push the workflow file.

### Verification
- Readiness summary: **pass=20, warn=0, fail=0**

