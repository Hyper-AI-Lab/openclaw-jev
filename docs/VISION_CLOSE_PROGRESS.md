# Vision close progress log

Append-only. Each plan step adds a dated entry below. Do not rewrite prior entries. Separate from `CONCEPT_TREE_PROGRESS.md`, `PRODUCT_RISK_PROGRESS.md`, and `INTEGRITY_AUDIT_PROGRESS.md`.

Plan: match the founding control plane and close the leftovers this VPS can host. Does **not** add Temporal Cloud, a second Temporal node, or other-host socket detection.

---

## 2026-09-27 — Step 1: Generic stop tells Kirill

- **Status:** complete
- **What landed:** `GenericTaskWorkflow._finish_stop` sets `stopped_by_user`, ends the process run, and sends one RMP ack (`Task … stopped as requested.`). A whole-message stop before or during the deliver child returns that ack and does not deliver the step result. Parent close policy terminates the child. A stop inside a longer sentence is still not a stop command.
- **Verification:** `tests/test_generic_task_conversational.py` `tests/test_stop_command.py` — 7 passed.
- **Remaining:** Steps 2–13.

---

## 2026-09-27 — Step 2: Timeout recovery is for this message

- **Status:** complete
- **What landed:** After a timed-out `POST /tasks`, recovery succeeds only when `GET /tasks/by-idempotency/{key}` finds an in-flight task for this message's key. Another session's active task no longer suppresses `intake_unavailable`. Terminal tasks with the same key are not treated as ownership.
- **Verification:** `tests/test_notify_user.py` — 8 passed. `cmp` plugin copies; `node --check` OK.
- **Remaining:** Steps 3–13.

---

## 2026-09-27 — Step 3: Intake outlives the client timeout

- **Status:** complete
- **What landed:** `POST /tasks` writes the task row with `intake_reserved` and commits before the slow intake call. A second POST with that key while the reservation is fresh returns the same row and does not start another intake. A reservation older than 150s can be continued. Early intake outcomes that belong to another task cancel the reservation. Plugin `POST /tasks` timeout is `llm + context + 75` seconds from settings (above the intake workflow budget).
- **Verification:** `tests/test_notify_user.py` `tests/test_task_idempotency.py` — 13 passed. `cmp` plugin copies; `node --check` OK.
- **Remaining:** Steps 4–13.

---

## 2026-09-27 — Step 4: A failed Slack notice is not an ack

- **Status:** complete
- **What landed:** `notifyRmpUser` returns failure unless the notice JSON has `delivered: true`. The idle-stop path logs an ack only in that case. Native Slack stays suppressed.
- **Verification:** `tests/test_notify_user.py` — 9 passed. `cmp` plugin copies; `node --check` OK.
- **Remaining:** Steps 5–13.

---

## 2026-09-27 — Step 5: Temporal production server on this Postgres

- **Status:** complete
- **What landed:** `temporal-dev` is disabled. `temporal.service` runs `temporalio/server:1.30.1` with Postgres databases `temporal` and `temporal_visibility` on this host. Frontend listens on `127.0.0.1:7233`. SQLite `temporal.db` was backed up to `data/backups/temporal-start-dev-20260927.db` and is not the live store. Worker requires `temporal.service`. Readiness checks the Postgres schemas, not SQLite WAL. Namespace `default` registered.
- **Verification:** `temporal operator cluster health` SERVING. Python client health ok. `/health` `temporal.ok` true. `tests/test_readiness.py::test_temporal_persistence_requires_postgres` passed. Worker active. 0 user tasks at cutover.
- **Remaining:** Steps 6–13.

---

## 2026-09-27 — Step 6: Evaluator web tools hit real backends

- **Status:** complete
- **What landed:** `:8791/v1/search` and `:8791/v1/jina` are real routes (LangSearch or Brave, and Jina reader). The evaluator uses those routes and treats a body `ok: false` as a failed tool. Chat/`none` still does not call them.
- **Verification:** `tests/test_evaluator_tools.py` — 12 passed. Live `GET /v1/search` returns HTTP 200 with a JSON body, not 404.
- **Remaining:** Steps 7–13.

---

## 2026-09-27 — Step 7: Advertised crawl matches the code

- **Status:** complete
- **What landed:** Crawl4AI requests with `max_pages` or `depth` above a single page run a bounded same-host crawl (cap 8 pages, depth 2). Crawlee status says in-memory Python BFS and `durable: false`. Node Crawlee is not reported as available.
- **Verification:** `tests/test_web_stack_crawl.py` — 2 passed (`max_pages=2` fetches twice).
- **Remaining:** Steps 8–13.

---

## 2026-09-27 — Step 8: Obscura actions are real or omitted

- **Status:** complete
- **What landed:** When CDP is set, fetch/goto/read plus click, type, and press go through Playwright. Click/type/press without CDP return `ok: false`. Catalog step prompts omit `obscura_browse` when CDP is down. Slack still does not call `:9222`.
- **Verification:** `tests/test_obscura_actions.py` — 2 passed. `tests/test_web_capability.py` — 15 passed. `node --check` on aura_web. Plugin copies `cmp` identical.
- **Remaining:** Steps 9–13.

---

## 2026-09-27 — Step 9: ScrapeGraph and browser-use can call a model

- **Status:** complete
- **What landed:** `aura-web-backends.service` loads `/etc/openclaw/openclaw.env`. ScrapeGraph without a key returns `ok: false` and does not fill schema fields with null. browser-use already failed closed without a key.
- **Verification:** `tests/test_web_stack_llm.py` — 1 passed. After restart, `GET /health` is ok.
- **Remaining:** Steps 10–13.

---

## 2026-09-27 — Step 10: Safe Harbor catalog is only real scanners

- **Status:** complete
- **What landed:** Catalog skips `moltbook_continuous`, swarm simulators, and other scripts whose header says placeholder or simulating. When Safe Harbor has a copy, the workspace duplicate is not listed. `scanner_auto_restart` stays off. Scanners are not on the DM path.
- **Verification:** `tests/test_scanner_catalog.py` — 1 passed. Live ids: real scans and `real_moltbook_scanner` only.
- **Remaining:** Steps 11–13.

---

## 2026-09-27 — Step 11: Cron must not rewrite OpenClaw dist

- **Status:** complete
- **What landed:** Root cron now runs `ensure_peripheral_daemons.js` (watchdog and self-auditor only). `restore_powers.js` exits without writing files. Neither file mentions `openclaw/dist`.
- **Verification:** `ops/verify_openclaw_patch.sh` passed. `grep openclaw/dist` on both cron scripts found nothing.
- **Remaining:** Steps 12–13.

---

## 2026-09-27 — Step 12: Docs match the live path

- **Status:** complete
- **What landed:** ARCHITECTURE names `temporal.service` (Postgres, one frontend) instead of `temporal-dev`. Memory canary schedule is the :37 hours. CONCEPT_TREE §12 lists only other-host sockets, historical `nvidia:unknown` day buckets, and no second Temporal node / no Cloud account. README already said gpt-5-nano primary. Nested Cursor rules stayed identical.
- **Verification:** search of ARCHITECTURE and README found no `start-dev`, `temporal-dev`, or MiniMax-as-primary. `cmp` of the architecture rules and both plugin copies matched.
- **Remaining:** Step 13.

---

## 2026-09-27 — Step 13: Production close of this plan

- **Status:** complete
- **What landed:** Live checks follow `temporal.service` instead of `temporal-dev`. SQLite vacuum no longer restarts the dev server. Gateway, rmp-api, and rmp-worker were restarted with 0 user tasks. Temporal and the web backends were already on the new units.
- **Verification:** `./venv/bin/pytest -q --ignore=tests/integration` — **371 passed, 3 skipped** (web-stack adapter tests skip in the RMP venv; they passed under the web-stack venv). `make production-check` — readiness **22 pass / 2 warn / 0 fail** (OTLP unset; health canary 2.2h old). Patch verify OK. Primary `openai/gpt-5-nano`. Intake conversational, latency 11s. `/health` `status=ok`, `temporal.ok=true`. Plugin copies and nested Cursor rules match.
- **Remaining (physical, not unfinished code):** other-host Slack sockets; historical day-bucket `nvidia:unknown` totals; no second Temporal node and no Temporal Cloud account on this VPS.




 Not a second node and not Temporal Cloud.




