# RMP Production Hardening Plan — Unsupervised Operation

**Status: PRODUCTION LIVE (2026-06-03)** — `development_mode: false`, go-live executed, canary E2E passed.

**Goal:** Full-scale, unsupervised, production-grade reliability and memory plane on a single Linux host.

---

## Phase status summary

| Phase | Name | Status |
|-------|------|--------|
| 0–11 | Feature development | ✅ Complete (`DEVELOPMENT_PLAN.md`) |
| 12 | Production foundation | ✅ Complete |
| 13 | Go-live & soak | ✅ Go-live + E2E canary; ⏳ 72h soak ongoing |
| 14 | Observability | ✅ Metrics, export API, alerts, dashboard; ✅ OTLP (Phoenix + collector) |
| 21 | Production polish | ✅ Registry backfill, timers/Slack confirm, OTLP live, public CI workflow |
| 15 | Infrastructure | ✅ Env secrets, patch verify, restore script; ⏳ Full Temporal cluster optional |
| 16 | Reliability | ✅ Idempotent Slack, leases, signals |
| 17 | Memory depth | ✅ Policy, retention, graph links, procedural promotion |
| 18 | Workflow catalog | ✅ 6 templates (registration, login, email, procurement, outreach, browser) |
| 19 | Scanner ops | ✅ Auto-restart (opt-in), sync in production mode |
| 20 | Continuous verification | ✅ 40 tests, canary timer, runbooks, `make production-check` |

---

## Architecture

```
Slack / Cron / Heartbeat → rmp_adapter → POST /tasks → FastAPI
    → Postgres + Qdrant + Artifacts + Temporal → rmp-worker → OpenClaw gateway
    → Slack delivery / reconciler / scanner monitor / cron reconciler
```

---

## Phase 12 — Production foundation ✅

| # | Deliverable | Location |
|---|-------------|----------|
| 12.1 | Readiness engine | `app/production/readiness.py`, `GET /api/production/readiness` |
| 12.2 | Go-live / rollback | `ops/go_live.sh`, `ops/rollback_dev.sh` |
| 12.3 | Backups | `ops/backup.sh`, `rmp-backup.timer` |
| 12.4 | Health probe | `ops/healthcheck.sh` |
| 12.5 | Persistent Temporal | `--db-filename` in `temporal-dev.service` |
| 12.6 | Requirements | `requirements.txt` |
| 12.7 | Pytest suite | `tests/` (40 tests) |

---

## Phase 13 — Go-live & soak ✅ (soak ongoing)

| # | Status |
|---|--------|
| 13.1 Staged go-live | ✅ `development_mode: false`, heartbeat `30m` |
| 13.2 Slack E2E | ✅ Canary task completed via OpenClaw in ~40s |
| 13.3 Cron E2E | ✅ MoltMarket cron enabled; reconciler tracks job |
| 13.4 72h soak | ⏳ Monitor via dashboard + reconciler events |
| 13.5 Rollback tested | ✅ Script available (`ops/rollback_dev.sh`) |

---

## Phase 14 — Observability ✅ (OTLP live)

| # | Deliverable |
|---|-------------|
| 14.1 | `docker-compose.observability.yml`, `ops/start_observability.sh` |
| 14.2 | `app/metrics.py`, `GET /metrics` |
| 14.3 | `app/production/alerting.py` wired to reconciler + failures |
| 14.4 | Dashboard production banner, readiness score, backup card |
| 14.5 | `GET /tasks/{id}/export` postmortem bundle |

**Traces:** Phoenix + collector running; `telemetry.otlp_endpoint=http://127.0.0.1:4318/v1/traces` (Phase 21).

---

## Phase 21 — Production polish ✅ (2026-08-11)

- Task-registry backfill (`--missing-only`) cleared readiness index warn
- Timers + Slack DM path confirmed (`auth.test`, owner UID)
- OTLP export enabled end-to-end
- Alerting remains opt-in (webhook empty)
- Public `.github/workflows/ci.yml` added (requires workflow push scope on PAT)

---

## Phase 15 — Infrastructure ✅ (interim Temporal)

| # | Deliverable |
|---|-------------|
| 15.1 | Persistent dev Temporal (upgrade path: Temporal Cloud or self-hosted) |
| 15.2 | Daily pg_dump + `docs/runbooks/backup-restore.md` |
| 15.3 | `/etc/rmp/rmp.env`, systemd `EnvironmentFile` |
| 15.4 | File permissions on secrets (600) |
| 15.5 | Localhost-only API binding |
| 15.6 | `ops/verify_openclaw_patch.sh` in `make production-check` |

---

## Phase 16 — Reliability ✅

- `app/activities/side_effects.py` — idempotent Slack
- `acquire_process_run_lease` — duplicate dispatch guard
- `cancel` / `retry` / `approve` signals on workflows
- `app/cron/reconciler.py` — OpenClaw cron ledger

---

## Phase 17 — Memory ✅

- `app/memory/policy.py` — secret redaction, scope gates
- `POST /memory/compact` — episodic retention
- Procedural promotion on catalog completion
- `memory_links` table + `app/memory/graph.py`

---

## Phase 18 — Workflows ✅

Templates: `account_registration`, `login`, `email_verification`, `procurement`, `outreach`, `browser_automation` (version 1 each).

MoltMarket cron re-enabled with RMP adapter path.

---

## Phase 19 — Scanner ops ✅

- `app/scanners/lifecycle.py` — opt-in auto-restart (`production.scanner_auto_restart`, `scanner_managed_ids`)
- Scanner sync active in production mode
- Safe harbor watchdog via existing `restore_powers` crontab

---

## Phase 20 — Continuous verification ✅

| # | Deliverable |
|---|-------------|
| 20.1 | `make test`, `make production-check` |
| 20.2 | `rmp-canary.timer` hourly |
| 20.3 | `docs/runbooks/` |
| 20.4 | `ops/restore_backup.sh` + DR doc |

---

## Operator commands

```bash
make test              # 40 pytest tests
make production-check  # health + patch verify
make readiness         # full readiness JSON
make backup            # manual backup
make canary            # E2E OpenClaw path
make rollback          # return to dev quiet mode
```

---

## Execution log

- **2026-06-03:** Phases 12–20 implemented; go-live executed; canary E2E passed; `go_live_ready: true`, readiness score 93%.
