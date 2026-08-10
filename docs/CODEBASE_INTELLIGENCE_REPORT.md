# Aura / RMP Codebase Intelligence Report

**Date:** 2026-06-08  
**Scope:** `/root/.openclaw/rmp`, `/root/.openclaw`, `/root/.openclaw/plugins/rmp_adapter`  
**Evidence:** Direct repository inspection, systemd units, 164 pytest tests, live DB/log correlation.

---

## 1. Executive Summary

**Aura** is a personal AI assistant for Kirill (Slack DM) built as a three-layer stack on a single VPS:

1. **OpenClaw** — Slack gateway, Kimi K2.6 via NVIDIA, JSONL sessions, workspace files (`USER.md`, `MEMORY.md`)
2. **RMP (Reliability & Memory Plane)** — FastAPI + Temporal + Postgres + Qdrant sidecar that owns task lifecycle, memory, LLM quota, and Slack delivery
3. **Safe Harbor** — legacy scanner watchdog (peripheral)

User Slack messages are **never answered by the main OpenClaw session directly**. The `rmp_adapter` plugin intercepts DMs, creates RMP tasks, runs Temporal workflows, dispatches to isolated `rmp_task_*` OpenClaw sessions, then delivers replies via `notify_slack_user`.

Production mode is live (`development_mode: false`). Primary failure modes observed: **timezone mismatch (server Europe/Berlin vs user JST)**, **reconciler killing long-running workflows** (15m threshold vs frozen `tasks.updated_at`), and **Temporal sandbox violations** when workflows call `load_settings()`/`os.environ`.

---

## 2. Repository Map

| Path | Purpose |
|------|---------|
| `/root/.openclaw/rmp/app/` | Python application core |
| `/root/.openclaw/rmp/ops/` | Shell/Python operational scripts |
| `/root/.openclaw/rmp/tests/` | Pytest (37 files, 164 tests) |
| `/root/.openclaw/rmp/worker.py` | Temporal worker entry |
| `/root/.openclaw/rmp/data/` | Runtime state (Qdrant, Temporal SQLite, quota, canaries) |
| `/root/.openclaw/plugins/rmp_adapter/` | Slack→RMP bridge (Node) |
| `/root/.openclaw/openclaw.json` | Gateway config |
| `/root/.openclaw/workspace/` | Agent workspace (USER.md, MEMORY.md) |
| `/root/.openclaw/agents/main/sessions/` | JSONL session transcripts |

---

## 3. System Architecture

```
Slack DM → rmp_adapter (sync POST /tasks)
         → RMP API (intake funnel, memory prefetch)
         → Temporal (GenericTaskWorkflow | CatalogTaskWorkflow)
         → Activities: plan, send_to_openclaw, evidence, notify_slack_user
         → OpenClaw hooks/agent (isolated rmp_task session)
         → Slack DM (idempotent chat.postMessage)
```

Background: reconciler (60s), canary timers, janitor, LLM quota broker (2 concurrent slots, 3 NVIDIA keys).

---

## 4. Execution Flows

### Slack user message
1. `message_received` hook → `routeSlackDmToRmp` → `POST /tasks`
2. Intake: fast path → skip_valid/noop/supersede → vector gate → LLM classify
3. `start_task_workflow` → `GenericTaskWorkflow`
4. Plan (deterministic for simple chat) → child `GenericExecuteChildWorkflow` per step
5. `send_to_openclaw` → JSONL poll (up to 600s)
6. Evidence + optional quality LLM → `notify_slack_user` → memory promotion + registry index

### Confirmed bug paths (2026-06-08)
- Workflow called `get_rework_max_attempts()` → `os.environ` → sandbox crash (fixed: pass via payload)
- `tasks.updated_at` not refreshed during long OpenClaw poll → reconciler stuck repair at 15m
- OpenClaw `Current time` uses server TZ (Europe/Berlin) — contradicts USER.md (JST)

---

## 5. Domain Model

| Entity | States | Notes |
|--------|--------|-------|
| Task | created, running, completed, failed, compensated, pending_user_input, stopped_by_user | User-visible unit |
| ProcessRun | created, running, waiting_agent, completed, failed_terminal, … | Execution instance |
| TaskIntakeDecision | audit of intake funnel | |
| TaskRegistryEntry | vector-indexed terminal task summaries | |

Business rules: intake never skips health canary; internal tasks suppress Slack; evidence gate can block LLM completion claims.

---

## 6. Data Model

Postgres via SQLAlchemy (`app/db/models.py`). Migrations inline in `app/db/database.py` (no Alembic). Qdrant collections: `rmp_memories`, `rmp_task_registry`. Temporal dev SQLite at `data/temporal.db`.

---

## 7. API / Interface Map

See `app/api/server.py`: `/tasks`, `/tasks/intake/preview`, `/memory/*`, `/api/llm/*`, `/api/production/readiness`, dashboard `/`.

Plugin: sync `POST /tasks` with `X-RMP-API-Key`. OpenClaw: `POST /hooks/agent`.

---

## 8. Configuration and Environment

| File | Role |
|------|------|
| `settings.json` | RMP: intake_mode, llm_quota, vector_memory, telemetry |
| `openclaw.json` | Model, auth profiles, Slack, plugins |
| `/etc/rmp/rmp.env` | DATABASE_URL, RMP_API_KEY |
| `/etc/openclaw/openclaw.env` | NVIDIA_API_KEY x3 |
| `workspace/USER.md` | Kirill profile, **JST timezone** (not wired to OpenClaw TZ until Phase 7) |

---

## 9. Build, Test, Deployment

```bash
cd /root/.openclaw/rmp && make test          # pytest
make production-check                         # health + skills + patch
make restart-rmp                              # restart api/worker/gateway
```

systemd: `rmp-api`, `rmp-worker`, `openclaw-gateway`, `temporal-dev`, timers for canary/backup/janitor.

No CI/CD in repo.

---

## 10. Testing Assessment

164 pytest tests cover intake, orchestration, memory, reconciler, canaries. Gaps: no E2E Slack integration test; no timezone regression test (added Phase 7); limited workflow sandbox tests.

---

## 11. Security and Reliability Assessment

**Confirmed:**
- API key auth on `/tasks`
- Slack delivery idempotency via `side_effect_receipts`
- Secrets in `/etc/*` env files (also api_key in settings.json — debt)

**Risks:**
- Single VPS, Temporal dev server, no HA
- Reconciler false-positive stuck repair on long tasks
- Rate limit storms on NVIDIA keys during concurrent gateway + worker load

---

## 12. Technical Debt

- Inline SQL migrations
- Hardcoded `/root/.openclaw` paths
- `get_rework_max_attempts()` still in catalog workflow (Phase 7 fix)
- Heavy post-completion work before Slack (partially fixed: notify moved earlier)
- Legacy `fix_*.py` scripts at repo root

---

## 13. Change Guide

- **New API route:** `app/api/server.py`
- **New workflow step:** `app/workflows/generic_task.py` + activity in `app/activities/`
- **Intake rule:** `app/task_registry/recurrence.py` or `intake_decision_engine.py`
- **Slack policy:** `app/notification_policy.py`
- **Deploy:** edit code → `make test` → `make restart-rmp`

---

## 14. Questions and Unknowns

- Exact Slack client delivery latency under socket-mode pong timeouts (observed warnings)
- Whether Kimi receives USER.md bootstrap on every `rmp_task_*` turn after first message

---

## 15. Evidence Index

| Claim | Evidence |
|-------|----------|
| Slack routing | `plugins/rmp_adapter/index.js:407-420` |
| Intake enforce | `settings.json` task_registry.intake_mode |
| Workflow sandbox error | Task `69485ea8` event `task.compensated`, worker log `os.environ restricted` |
| Timezone | `workspace/USER.md:8`, no userTimezone in `openclaw.json` |
| Stuck repair | `reconciler.py:18-19`, task `0a1f42a4` event `reconciler.stuck_repaired` |
| Tests count | `.pytest_cache/v/cache/nodeids` (164) |
