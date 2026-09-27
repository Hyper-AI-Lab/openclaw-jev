# Product-risk close progress log

Append-only. Each plan step adds a dated entry below. Do not rewrite prior entries. Separate from `CONCEPT_TREE_PROGRESS.md` and `INTEGRITY_AUDIT_PROGRESS.md`.

Plan: close named residual product/ops risks on this VPS. Does **not** mean the whole Aura roadmap is finished.

---

## 2026-09-09 — Step 1: `message_received` fail-closed

- **Status:** complete
- **What landed:** Slack `message_received` now returns `{ handled: true }` after skip-already-claimed, after `routeSlackDmToRmp`, and in `catch` (same fail-closed as `inbound_claim` / `before_dispatch`). Dual-written `rmp_adapter`. Never native.
- **Verification:** `./venv/bin/pytest -q tests/test_notify_user.py` — 7 passed. `cmp` plugin copies identical; `node --check` OK.
- **Remaining:** Steps 2–9.

---

## 2026-09-09 — Step 2: Temporal WAL + systemd recover path

- **Status:** complete
- **What landed:** `temporal-dev` uses `--sqlite-pragma journal_mode=WAL` + `synchronous=NORMAL`. `StartLimitIntervalSec` moved to `[Unit]`. Watchdog is one `ExecStart` (`healthcheck --recover || temporal_recover.sh`), not broken `ExecStartPost`. Worker `Requires=temporal-dev` and waits on gRPC (`ops/wait_temporal.sh`). Backup uses SQLite backup API; restore stops `temporal-dev` before replacing the file. Units tracked in `ops/systemd/`. ARCHITECTURE stuck-repair is 45m (matches code).
- **Verification:** Live `PRAGMA journal_mode=wal` (`temporal.db-wal` present). `systemd-analyze verify` OK. `tests/test_readiness.py` — 7 passed. Idle restart of temporal-dev + rmp-worker (0 active user tasks).
- **Remaining:** Steps 3–9.

---

## 2026-09-09 — Step 3: Temporal clients and health

- **Status:** complete
- **What landed:** `connect_temporal_with_retry` (bounded backoff) used by `worker.py`. Reconciler `_get_temporal()` health-checks the cached client and reconnects. `/health` includes `temporal.ok` and is `degraded` if Temporal is unreachable. Persistence check still requires WAL.
- **Verification:** `./venv/bin/pytest -q tests/test_temporal_connect.py` — 3 passed.
- **Remaining:** Steps 4–9.

---

## 2026-09-09 — Step 4: User vs canary quota pools

- **Status:** complete
- **What landed:** `classify_slot_kind` stamps `user`/`canary`/`heartbeat` from tags/`task_type`. `max_concurrent=3` splits into 2 user + 1 canary. Canary reserve fails immediately if the canary slot is full (no 1800s wait). User reserve preempts the canary slot and `cancel_task_sync`s the canary Temporal workflow. Execute pass-through tags from `send_to_openclaw`. Hourly/memory canary skip if user LLM slots are busy. Idle still 5s. Users still not `wait_active` onto canaries.
- **Verification:** `tests/test_quota_broker.py` `tests/test_llm_orchestration.py` `test_wait_active_targeting_canary_is_ignored_for_user_dm` — 27 passed.
- **Remaining:** Steps 5–9.

---

## 2026-09-09 — Step 5: Memory canary honesty

- **Status:** complete
- **What landed:** Transcripts via `get_session_entry` / `read_transcript_lines` (SQLite), not leftover `sessions.json`. Missing transcript → `inconclusive` (not CANARY OK). `completed` with `memory_ok=0` is unproven (readiness warn + sentinel issue). Timeout with transcript_ok still not treated as a memory failure. No Slack on CANARY_OK.
- **Verification:** `tests/test_canary_sentinel.py` `tests/test_readiness.py` — 21 passed.
- **Remaining:** Steps 6–9.

---

## 2026-09-09 — Step 6: Galaxy / Obscura / Safe Harbor fail-soft

- **Status:** complete
- **What landed:** WEB CAPABILITY BRIEF omits `obscura_browse` when CDP is down (`obscura.available` stamped). Chat/`none` still get no brief. Evaluator probes `:8791` only for real web classes; stack-down is `ok: False`. Plugin curl `--connect-timeout 8` (long `--max-time` only after connect). Safe Harbor stays peripheral; readiness warns if `scanner_auto_restart` is on. Leftover Docker name `aura-obscura` removed while idle; unit not started; Slack path does not require Obscura.
- **Verification:** `tests/test_web_capability.py` `tests/test_evaluator_tools.py` `tests/test_readiness.py` — 32 passed. `cmp` aura_web `client.js` copies; `node --check` OK.
- **Remaining:** Steps 7–9.

---

## 2026-09-09 — Step 7: Usage `nvidia:unknown` honesty

- **Status:** complete
- **What landed:** `resolve_usage_profile_id` treats `{nvidia:unknown, unknown, ""}` as unset, then applies model/provider rules (`openai/`/`gpt-`/`text-embedding-` → `openai:default`; `nvidia/` → `nvidia:default`). Rewrites only `rolling_24h` events that have a model. Day-bucket counts left in place. Summary/healthcheck annotates unattributed historical days. Never invent `key2`/`key3`.
- **Verification:** `tests/test_usage_monitor.py` — 7 passed.
- **Remaining:** Steps 8–9.

---

## 2026-09-09 — Step 8: This-host Slack sockets runbook

- **Status:** complete
- **What landed:** [`docs/runbooks/slack-sockets.md`](runbooks/slack-sockets.md) — this-host only; other hosts undetectable; never `apps.connections.open`. `check_slack_sockets` unchanged (exactly one `openclaw-gateway` PID). ARCHITECTURE points at the runbook.
- **Verification:** `tests/test_readiness.py` slack_sockets tests still pass.
- **Remaining:** Step 9.

---

## 2026-09-09 — Step 9: Production close of this plan

- **Status:** complete
- **What landed:** Named leftover product/ops risks on this VPS are closed. `docs/CONCEPT_TREE.md` §12 now lists remaining residuals (single-node Temporal, other-host sockets undetectable, historical day-bucket `nvidia:unknown`, Galaxy/Safe Harbor still not the product). This does **not** finish the whole Aura roadmap. Idle restart: gateway + rmp-api/rmp-worker (0 user tasks). Temporal not restarted (WAL/unit already applied in Step 2).
- **Verification:** `./venv/bin/pytest -q --ignore=tests/integration` — **362 passed**. `make production-check` — readiness 22 pass / 2 warn / 0 fail (memory canary unproven; telemetry OTLP unset as before). Patch verify OK; primary `openai/gpt-5-nano`; intake conversational; latency 7s. `cmp` `rmp_adapter` and `aura_web/lib/client.js` copies identical; nested Cursor architecture rules identical. After restart: `/health` `status=ok`, `temporal.ok=true`, vector ready (`text-embedding-3-small`).
- **Remaining (honest leftovers):** Multi-node Temporal / Temporal Cloud; other-host Slack sockets; historical day-bucket `nvidia:unknown` totals; whole Aura product roadmap.




