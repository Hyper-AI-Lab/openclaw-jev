# Runtime efficiency progress (append-only)

Plan: gateway freeze behind the ~70 s intake stall, idle token burn, and related fixes.
Each step appends one entry below. Earlier entries are never rewritten.

---

## Step 1 — Safe code reloads (2026-09-28)

- **Change:** `ops/reload_runtime_on_code_change.sh` now waits until
  `count_active_user_tasks_sync(strict=True) == 0` before restarting `rmp-api` and
  `rmp-worker`, polling every 15 s and logging a deferral once a minute. After 30 min it
  stops waiting and leaves the restart to the canary sentinel, which already restarts a
  stale runtime once no user task is active. A failed task lookup counts as busy.
- **Change:** `ops/watch_code_and_reload.sh` reloads only for `*.py` changes (it already
  skipped `__pycache__`/`.pyc`), names the changed file in the log, and drains events
  queued while a reload waited.
- **Change:** `count_active_user_tasks_sync` gained `strict=True`, which raises on a failed
  lookup; the sentinel keeps the default behaviour.
- **Tests:** `tests/test_code_reload.py` runs the script with stubbed `python`,
  `systemctl` and `curl`: idle restarts once, busy-then-idle restarts after waiting, still
  busy or unknown never restarts. Strict counting raises. 19 passed with the sentinel
  tests.
- **Live check:** watcher restarted with the new script. A non-`.py` file under `app/`
  caused no reload. Re-saving `app/production/canary_sentinel.py` while idle logged
  `change detected (app/production/canary_sentinel.py)`, restarted both units, health ok.

## Step 2 — Temporal unit cleanup (2026-09-28)

- **Finding:** `rmp-api.service`, `rmp-janitor.service` and `rmp-janitor-frequent.service`
  still had `Wants=temporal-dev.service` (and `rmp-temporal-vacuum.service` an `After=`), so
  every API restart launched the retired start-dev server, which crash-looped because the
  Postgres-backed `temporal.service` owns port 7233. None of these units had a repo copy.
- **Change:** added the four units to `ops/systemd/` with `After/Wants=temporal.service`
  (everything else copied verbatim), installed them to `/etc/systemd/system`,
  `daemon-reload`, and cleared the stale failed state of `temporal-dev.service`, which
  stays disabled.
- **Live check:** `temporal-dev` has no `WantedBy`/`RequiredBy`. An idle `rmp-api` restart
  produced no `temporal-dev` activity; `rmp-api`, `rmp-worker`, `temporal` active,
  `temporal-dev` inactive, health ok.

## Step 3 — Non-blocking Slack plugin (2026-09-28)

- **Finding:** on this build Slack DMs are routed by the plugin's `message_received` hook
  (OpenClaw fires it without waiting), not by `inbound_claim`. Its synchronous
  `curl POST /tasks` held the gateway's event loop for the whole intake (70–74 s per DM),
  so intake's LLM leg (`POST /hooks/agent` on the same gateway) could not run and timed out.
- **Finding:** `[slack] handler failed … reading 'catch'` was a version skew, not RMP code.
  `@openclaw/slack` was 2026.7.1 on core 2026.9.1. The core's inbound debouncer (`runFlush`)
  expects `onFlush` to return `{admission, completion}`; the 2026.7.1 plugin returns a
  Promise, so every DM threw after dispatch had already started. `@openclaw/brave-plugin`
  had the same lag, because `ops/upgrade_openclaw.sh` never updated npm-installed plugins.
- **Change (plugin, both copies identical):**
  - All RMP HTTP uses async `fetch` with `AbortSignal.timeout`. Deadlines are unchanged:
    `POST /tasks` gets llm + ctx + 45 + 30 s, notices 10 s, everything else 15 s. The
    active-task lookup gets 5 s, because it runs inside hooks with a 15 s budget.
  - The claim hooks mark the DM claimed and return `{handled: true}` at once. Routing runs
    in the background, one route at a time per session in arrival order, so a stop still
    sees the task it stops.
  - Timeout recovery keeps the idempotent re-POST and the `by-idempotency` lookup, with
    async 2/3/5 s sleeps, then sends `intake_unavailable`. Stop signalling and `stop_idle`
    are unchanged.
  - `before_message_write` stays synchronous. Cron routing (active-task check, then
    `POST /tasks`) runs after it returns `{block: true}`. The dev-mode assistant guard reads
    the last known active task, which `before_agent_run` refreshes. `message_sending` awaits
    its lookup.
  - The `python3` SQLite lookup is now an in-process `node:sqlite` read. No `child_process`
    remains.
- **Change (upgrade path):** `ops/upgrade_openclaw.sh` runs `openclaw plugins update --all`
  after the core install, which picks the newest version compatible with the core. It also
  records each plugin's prior version in `VERSIONS.txt` for rollback.
- **Tests:** `tests/node/rmp_adapter.test.js` (`node:test`, mocked `fetch`, 11 tests)
  covers:
  - an immediate claim while intake is pending;
  - timeout recovery, and the notice when intake never recovers;
  - a non-timeout failure;
  - stop, and an idle stop;
  - per-session ordering;
  - a synchronous `before_message_write`, and `message_sending`;
  - the network guard;
  - no process spawning, and live-copy parity;
  - the SQLite lookup (on this host only).

  `tests/test_notify_user.py` gains a check that neither copy blocks. CI runs the Node tests
  on Node 22. Full suite: 469 passed, 3 skipped; Node: 11 passed.
- **Incident (caused by me):** my first Node test runs put the real `fetch` and settings
  back while a failed test's background cron route was still running. Three `POST /tasks`
  reached the live API (11:38:57–11:39:20Z); intake asked for clarification, and three
  questions were DM'd to Kirill. I cancelled the three tasks with
  `POST /tasks/{id}/cancel`, which clears `next_check_at`, so no reminders go out. The tests
  now never restore the real `fetch` or settings, and a test reproduces the leak and shows it
  now fails closed. Later runs created no tasks and no Slack receipts.
- **Deploy (idle, approved):** with 0 active user tasks, I stopped the gateway and ran
  `openclaw plugins update --all`: slack and brave went from 2026.7.1 to 2026.9.1, and
  mistral was already 2026.9.1. Then I installed the plugin and started the gateway (down
  11:55:42–11:58:16Z). The same 5 plugins loaded and the Slack socket connected.
- **Live check:**
  - DM "Hi Aura, gateway check" at 12:19:09Z: `before_dispatch` saw the claim 18 ms after
    `message_received` (it had been 70–74 s).
  - Jev abstained. The LLM intake returned `create_guided` (confidence 78) at 12:19:38Z
    instead of timing out. The task was created at 12:19:48Z and the reply delivered at
    12:20:10Z.
  - A second identical DM at 12:31:52Z took the same path: the LLM chose `create_fresh` at
    confidence 60, and the low-confidence policy asked a clarifying question.
  - Neither DM produced `process was frozen` or `reading 'catch'`.
- **Still open:**
  - Intake's LLM leg lost about 17 s to two 5 s first-byte cuts on gpt-5-nano. Attempts 1
    and 2 were cut at 6.3 s and 5.2 s; attempt 3 answered in 5.4 s. Step 6 addresses this.
  - A separate freeze happens 50–70 s after every gateway start: 70–91 s long, RSS up by
    680–870 MB, nothing logged meanwhile (today at 09:28, 09:38, 11:49 and 14:01 CEST).
    Afterwards the first heartbeat run spent 14 s in prep on the 11,570-event heartbeat
    session, then failed after three first-byte cuts. It is not DM-related. I'll re-check at
    the Step 6 restart, after Step 5 archives that session.

## Step 4 — Broker waits (2026-09-28)

- **Finding:** `reserve_profile` held the process-wide asyncio `_lock` across its whole
  wait loop, and `release_profile` takes the same lock. One waiting reserve therefore
  blocked every other reserve and every release in that process until it gave up (up to
  `max_wait_sec` = 1800 s). A slot freed in the same process could not reach the waiter.
  Replaying the committed broker against a temp state with 2/2 user slots held, a release
  waited 3.9 s behind a waiter with a 4 s cap, and the waiter then timed out anyway.
- **Change (`app/llm/quota_broker.py`):**
  - The lock covers one attempt (the reservation and the profile pin); sleeps happen
    outside it.
  - `reserve_profile(deadline=...)` takes the caller's epoch deadline. The wait ends at the
    earlier of the deadline and the configured cap, and the last sleep is shortened to fit.
  - `_mutate_reserve(why=...)` reports why an attempt failed: user or canary slots full
    (with counts), every key cooling down, or a key still inside its pacing interval.
  - A reserve that waited more than 2 s logs `LLM reserve waited …s … last_reason=…` at
    INFO. Giving up logs `LLM reserve gave up after …s … reason=…` at WARNING, and the
    `TimeoutError` carries the reason.
- **Change (`app/activities/openclaw_activities.py`):**
  - `_activity_deadline()` reads the activity's own start-to-close budget from Temporal
    (`started_time` + `start_to_close_timeout`), minus 5 s for parsing and persisting.
  - Intake (`_execute_intake_llm`, all model attempts) and the evaluator
    (`_execute_on_internal_session`) pass it to `_dispatch_openclaw_session`, which uses it
    for the quota wait and to cap the reply poll. Before, a second intake model could poll
    past the 70 s activity limit.
  - The evaluator returns its usual `Error: …` when the quota wait runs out, as it already
    did for dispatch errors.
- **Tests (`tests/test_llm_orchestration.py`, 6 new):**
  - a release completes within 1 s while a reserve waits, and the waiter then gets the slot;
  - the caller's deadline ends the wait with the reason in both the error and the warning;
  - a 2.3 s wait is logged with `last_reason=user slots full`;
  - `_mutate_reserve` reports pacing and cooldown;
  - intake and the evaluator pass the activity deadline (evaluator degrades to `Error:`);
  - there is no deadline outside an activity.

  Broker suites: 20 passed. Full suite: 475 passed, 3 skipped.
- **Deploy:** the idle-aware reloader held the restart while intake's clarifying question
  for the second test DM was open (task `5d724ea3`, one active user task). With Kirill's
  approval I cancelled that test task via `POST /tasks/{id}/cancel`. The reloader then
  restarted `rmp-api` and `rmp-worker` at 12:44:16Z, health OK.
- **Observation, not changed here:** `_pick_key` ranks keys by load only. It can pick a key
  still inside its 5 s pacing interval while another key is free, and the attempt then
  waits. The new wait log will show how often that happens (`… paced (…s left)`).

## Step 5 — Heartbeat off (2026-09-28)

- **Research (OpenClaw 2026.9.1 dist and docs):**
  - `resolveHeartbeatIntervalMs` treats a zero interval as "no heartbeat". The heartbeat
    runs as a system-owned cron job, `heartbeat-main`, that OpenClaw rebuilds from config:
    with `every: "0m"` it is kept but set to `enabled: false`.
  - `openclaw sessions archive <key>` is the supported path. It is the same
    `sessions.patch` operation as the Control UI, keeps the transcript, marks the session
    archived, and drops it from the active list. Only an agent's main session is protected.
- **Change:** `apply_openclaw_policy` (`app/llm/model_policy.py`) enforces
  `agents.defaults.heartbeat.every = "0m"` and leaves the other heartbeat keys alone.
  `ops/upgrade_openclaw.sh` already runs it, so upgrades keep the heartbeat off. The test
  asserts the key and its change entry.
- **Applied:**
  - I backed up `openclaw.json`
    (`data/backups/openclaw.json.pre-heartbeat-off.20260928T124858Z`). A dry run on a copy
    changed only `agents.defaults.heartbeat.every`, and then I applied it live at 12:48:58Z.
  - The gateway hot-reloaded it: `[heartbeat] disabled`, then `config hot reload applied`.
    The Slack channel restarted and reconnected in 1 s. `openclaw cron list --all` shows
    `heartbeat-main enabled=False`.
  - The archive dry run returned `would_archive`. The real run archived
    `agent:main:heartbeat` (`archived_at` 12:50:15Z), and all 11,588 transcript events of
    session `4c67f9dd…` are kept.
  - I moved the stray `workspace/HEARTBEAT.md_append` (a 106-byte stock
    "reply HEARTBEAT_OK" line) to `data/backups/workspace/`, which is gitignored.
- **Docs:** CONCEPT_TREE §3.2 marks the heartbeat actor off, and §5.3 records the policy,
  the archive and the reason. The plugin's heartbeat note says it is off by policy; the
  guard stays. Both plugin copies are identical.
- **Tests:** `test_model_policy.py` and `test_notify_user.py`: 15 passed. Node: 11 passed.
- **Expected effect:** the heartbeat was 83.4M of 94M transcript tokens (89%): 48 runs a day,
  each re-billing a context that grew from 37k to 172k tokens, often several times after
  first-byte cuts. Step 7's accounting will measure the before/after.

## Step 6 — OpenAI first-byte allowance (2026-09-28)

- **Finding (dist `builtin-openclaw-zQV8Wwjr.js`):**
  - `streamWithIdleTimeout` used one 5 s window for three phases: stream creation (waiting
    for response headers), the first chunk, and every gap between chunks.
  - `resolveLlmFirstEventTimeoutMs` separately gave the transport's first-event guard
    (`@openclaw/ai`, openai-completions) `CLOUD_LLM_FIRST_EVENT_TIMEOUT_MS` = 5 s.
  - gpt-5-nano holds its response headers until the first token, so nearly every cut
    happened at creation.
  - `worker/worker.mjs` is the hashed cloud-worker bundle. Local turns don't run through it,
    and the existing patches never touched it, so this patch leaves it alone too.
- **Change (`patch_openclaw.sh`, Patch 6c, marker `RMP_OPENAI_FIRST_BYTE_20S`):**
  - `RMP_OPENAI_FIRST_BYTE_MS = 2e4`.
  - For `provider === "openai"`, the creation timer and the first iterator arm use
    `max(idle, 20 s)`, and `resolveLlmFirstEventTimeoutMs` returns 20 s, still capped by
    run and agent timeouts.
  - Later arms (gaps between chunks) keep the idle window, and non-OpenAI providers are
    byte-for-byte unchanged. The timeout message now shows the window that fired.
  - The patch applies all five edits or none, and prints `skip … (dist shape changed)` if
    any target moved. `require_marker` in the patcher and `check_present` in
    `ops/verify_openclaw_patch.sh` then fail loudly.
- **Pre-flight on a copy of the dist file:**
  - The patch applied, a second run changed nothing, and `node --check` passed.
  - I ran a harness on the extracted original and patched functions with stubbed
    dependencies and scaled windows (idle 100 ms, first byte 400 ms):

    | Case | Original | Patched |
    |------|----------|---------|
    | OpenAI, slow headers | cut | passes |
    | OpenAI, slow first chunk | cut | passes |
    | OpenAI, slow gap between chunks | cut | cut |
    | OpenAI, first chunk beyond the allowance | cut | cut |
    | NVIDIA, slow headers or first chunk | cut | cut |

  - Transport first-event timeout: OpenAI gets the allowance, NVIDIA keeps 5 s, and an
    agent timeout still caps it.
- **Applied:** `patch_openclaw.sh` patched 1 file and `verify_openclaw_patch.sh` passed.
  The gateway restarted while idle at 12:58:41Z; it logged `[heartbeat] disabled`, and
  Slack connected at 12:59:32Z.
- **Docs:** CONCEPT_TREE §4 and §8 state the rule (OpenAI 20 s first byte, NVIDIA 5 s,
  gaps 5 s for all). Both `rmp-architecture.mdc` copies carry the same sentence and are
  byte-identical.
- **Measured (gateway `[model-fetch]` log):**

  | | OpenAI requests | Idle cuts | Headers after |
  |---|---|---|---|
  | Before (27–28 Sep) | 340 | 183 (54%) at 5 s | 157 at p50 3,988 ms, p90 4,800 ms, max 4,998 ms (clipped by the cut) |
  | After, 8 tiny turns | 8 | 0 | 1.8–3.5 s |
  | After, 4 realistic intake previews | 6 | 0 | 5.9, 9.8, 2.2, 6.8, 5.6 and 3.7 s |

  - Four of the six intake calls would have been cut under the old rule.
  - Intake previews on the LLM path took 26.0, 14.8 and 17.1 s; Jev decided the fourth
    in 1.9 s.
- **New findings, outside this step:**
  - **Dead fallback:** NVIDIA returns 410 for `minimaxai/minimax-m3` on every call: "reached
    its end of life on 2026-09-09T09:00:00Z" (10/10 calls on 27–28 Sep). Intake's fallback
    attempt (`…_fb1`) failed this way during the measurement. The configured chain
    gpt-5-nano → MiniMax has had no working fallback since 9 Sep. This bears on Step 9.
  - **Startup freeze:** it is not the heartbeat. With the heartbeat disabled and its session
    archived, this start still froze for 101 s (13:00:30–13:02:11Z, RSS +~700 MB), about
    66 s after `listening`. It happens on restarts only, not on DMs.
- **Decisions (Kirill, after this step):** replace MiniMax with `nvidia/openai/gpt-oss-20b`
  (added as its own step before Step 9). Investigate the startup freeze as an extra step
  after the plan.

## Step 7 — Token visibility (2026-09-28)

- **Finding:**
  - `app/llm/usage_monitor.py` scraped `sessions/*.jsonl`, which OpenClaw 2026.9 no longer
    writes, so gateway LLM turns went unrecorded. The broker's balanced key choice read
    those empty per-profile totals.
  - In the SQLite store (`transcript_events`), an aborted attempt carries zero usage, so the
    prompts re-sent after 5 s cuts appeared nowhere. That's why the OpenAI dashboard ran far
    above what transcripts showed.
- **Change (`app/llm/usage_monitor.py`):**
  - `scrape_openclaw_sessions()` now reads `transcript_events`: it resumes from the last
    rowid, starts 24 h back on its first run, and writes the store once per scrape (the old
    path wrote it twice per event). Session keys come from `session_windows`.
  - `transcript_usage(hours)` reports per UTC day and per category: heartbeat, intake,
    evaluator, task, canary (from the task row, by the broker's canary rule), cron,
    slack_main, other. For each it counts attempts, aborted, input, cache-read, output and
    `aborted_prompt_tokens`, and it reports the largest live context.
    - `aborted_prompt_tokens` takes the prompt of the next successful call in the same
      session (retries resend it), else the previous one. It is an upper bound on
      re-billing, since not every aborted request is billed.
    - "Live" means an unarchived session with a successful call in the window.
  - `usage_alerts()` checks three things:
    - 24 h prompt tokens sent over budget (`llm_usage.input_budget_24h`, default 5M);
    - a live context above 60,000 tokens;
    - an abort rate above 10% (at least 20 attempts).

    The budget and abort-rate breaches must still hold in the last 2 h
    (`RECENT_WINDOW_HOURS`), so a burn that already stopped does not re-alert every 4 h
    until it leaves the 24 h window.
  - `get_summary()` (the `/api/llm/usage` endpoint and `ops/llm_usage_report.py`) adds
    `transcripts_24h` and `transcript_alerts`. The JSONL-only helpers the port orphaned are
    removed.
- **Change (sentinel and production-check):** `evaluate_llm_usage()` joins
  `evaluate_canaries()` and alerts through the existing ops Slack path with its 4 h
  cooldown. `ops/healthcheck.sh` (the first stage of `make production-check`) prints an
  `llm_transcripts_24h:` line, one line per category, and a `WARN: llm_usage …` per breach.
- **Tests:** 9 new in `test_usage_monitor.py` against a temp store shaped like OpenClaw's,
  covering:
  - categories and canary vs task;
  - aborted-prompt attribution, next and previous;
  - the window cut-off and archived sessions excluded from live context;
  - the missing store;
  - alert thresholds, including a stopped burn and small samples;
  - the scraper resuming without double counting.

  3 new in `test_canary_sentinel.py` (an ongoing burn, a stopped burn, a large context). The
  two `run_sentinel` tests stub the new check, as they already stub runtime sync. Full
  suite: 484 passed, 3 skipped.
- **Live attribution (last 48 h, before the fixes had aged out):**

  | Category | Attempts | Aborted | Prompt recorded | Prompt sent on aborts |
  |---|---|---|---|---|
  | heartbeat | 307 | 227 | 13.3M | 37.7M |
  | canary | 63 | — | 1.20M | — |
  | intake | 46 | — | 0.93M | — |
  | task | 10 | — | 0.23M | — |
  | evaluator | 9 | — | 0.07M | — |

  The dashboard's 13.08M/day peak sits between recorded prompts (about 7M/day) and recorded
  plus aborted-sent (about 26M/day), so part of the aborted prompts was billed. The largest
  live context is now 40.9k (an intake session); the heartbeat's 172k session is archived.
- **Alert timing:** at 14:03Z the check still saw pre-fix data (24 h: 27.3M sent, 57%
  aborts; last 2 h: 11 of 29 aborted). Every OpenAI idle cut in those 2 h came before the
  12:58Z patch; the rest was the 12:33Z heartbeat and one MiniMax 410. Kirill is informed
  here, so I recorded the `llm_usage` incident in the sentinel's alert state (its normal 4 h
  cooldown). The pre-fix data leaves the 2 h window by about 14:34Z.
- **Incident (caused by me):** sourcing `/etc/openclaw/openclaw.env` with `set -a` for the
  NVIDIA probes left every key in that file exported in my shell. One later pytest run then
  called the live Jev API, and `test_intake_deterministic` got confidence 100 instead of 0.
  I unset all of the file's variables by name (nothing was printed), and the suite passes
  clean. From now on, keys are read into one-command subshells only.

## Step 8 — Registry on every terminal path (2026-09-28)

- **Finding:**
  - `reconcile_once` sets terminal statuses itself on three paths, commits once at the end,
    and never indexes. The paths are orphaned-reply recovery (`completed`), a stale task
    whose workflow completed, and a stale task whose workflow failed, was terminated or
    was cancelled.
  - Stuck repair already indexes, through `finalize_task_failure` and `record_compensation`
    in `db_activities`.
  - `POST /tasks/{id}/cancel` is a fourth terminal path without indexing.
  - September coverage before the fix: user tasks 2 of 20 indexed, and tasks finalized by
    the reconciler 0 of 11.
- **Change:**
  - `reconcile_once` collects the tasks it finalizes and indexes each after its single
    commit (the indexer reads the committed row). `stats["registry_indexed"]` counts them.
  - The cancel endpoint indexes after its commit.
  - `ops/backfill_task_registry.py` gains `--task-id` (repeatable) to index exactly the given
    tasks.
- **Tests:**
  - `test_reconciler_indexes_the_tasks_it_completes_after_commit` covers the orphaned-reply
    and stale-completed paths. It asserts the call order commit, index, index.
  - `test_task_cancel_api.py` asserts commit, then index.
  - Full suite: 486 passed, 3 skipped.
- **Backfill (September, real user work):** 13 tasks via `--task-id`, run with the worker's
  env files sourced only in the child process: 13 indexed, 0 errors, all with vectors.
  - Indexed: the 11 reconciler completions (5 Sep, 27 Sep and 28 Sep), plus 2 tasks
    cancelled through the API (5 Sep, and today's second test DM).
  - Deliberately left out:
    - my 6 accidental "summarize inbox" rows from the test leak (3 user, 3 cron);
    - an intake attach-cancellation (merged into another task);
    - an intake placeholder row;
    - 40 September canaries. The normal path indexes canaries, so 715 of 755 are already
      there. These are health checks that intake then cites as prior work.
  - After: reconciler-finalized tasks 11 of 11 indexed; user tasks 15 of 20, with the
    remaining 5 being the rows excluded above.
- **Deploy:** the idle-aware reloader restarted `rmp-api` and `rmp-worker` at 14:12:56Z,
  health OK.

## Extra step — Spurious Temporal full recoveries (2026-09-28, approved by Kirill)

- **Finding:** the gateway PID changed at 13:11Z with no action of mine. The cause was
  `rmp-temporal-watchdog` (every 5 min, `temporal_healthcheck.sh --recover ||
  temporal_recover.sh`).
  - At 13:10:55Z the probe printed `Temporal health: ok`, then its Python process aborted
    during interpreter finalization (`Fatal Python error: PyGILState_Release … finalizing`,
    core dumped). That is the Temporal SDK's native threads outliving shutdown.
  - The `||` read the abort as unhealthy and ran the full recovery. It stops the API and
    worker without checking for user work, runs `temporal_purge_running.py
    --force-recovery`, and restarts Temporal, the API, the worker and the gateway.
  - It happened 19 times in 7 days, each paired with the same abort. Today it ran at 06:30,
    09:25, 09:35, 11:45 and 15:10 CEST. That accounts for the gateway restarts, and their
    70–100 s startup freezes, that Step 3 attributed to other causes.
  - It very likely also caused in-flight workflows to vanish. The reconciler finished 11
    September user tasks by orphaned-reply recovery, for example the 07:44Z DM on 27 Sep
    with a recovery at 07:54Z.
- **Change:**
  - `ops/temporal_healthcheck.py` flushes and ends with `os._exit(code)` once the verdict is
    known, skipping the finalization that aborts.
  - `ops/systemd/rmp-temporal-watchdog.service` runs `temporal_recover.sh` only when the
    probe exits 1, which is its own "still unhealthy after the soft restart" verdict. Any
    other code passes through as a failed unit without touching the stack. `$` is escaped
    as `$$` for systemd.
  - Installed to `/etc/systemd/system` and ran `daemon-reload`; `systemd-analyze verify`
    is clean.
- **Tests (`tests/test_temporal_watchdog.py`, 6):**
  - The real `ExecStart` command runs against stub scripts: exit 1 recovers; exits 0, 2
    and 134 do not, and pass through.
  - The probe runs against a fake `temporalio` whose client registers an exit hook that
    aborts, standing in for the finalization crash. The probe still exits 0 with `ok`.
    With the old probe restored, this test fails.
  - An unreachable Temporal exits 1.
- **Live check:** a manual run at 14:24:39Z printed `ok` and the unit finished successfully,
  with no recovery. All services active.
- **Not changed (noted):** `temporal_recover.sh` itself still stops user work without an
  idle check and backs up the retired `data/temporal.db`. With the false trigger gone, it
  runs only when Temporal is really down.

## Extra step — Replace the dead MiniMax fallback (2026-09-28, approved by Kirill)

- **Finding:**
  - NVIDIA returns 410 for `minimaxai/minimax-m3` ("end of life on 2026-09-09"), and
    MiniMax is gone from the catalog (81 models listed).
  - A live tool-call probe on this account:

    | Model | Result |
    |---|---|
    | `openai/gpt-oss-20b` | 200, correct tool call, 2.2–2.5 s |
    | `nemotron-3-ultra-550b` | 200 in 6.4 s |
    | `nemotron-3.5-lightning` | 200 in 22.8 s |
    | `kimi-k3` | timed out at 60 s |
    | `kimi-k2.6`, `mistral-large`, `nemotron-nano-3` | not available to this account |
    | `nemotron-3-super` | 500 |

  - Kirill chose `nvidia/openai/gpt-oss-20b`. History check: GPT-OSS 20B was intake and
    subagent backup on 10 Aug, then dropped the same day for "DeepSeek V4 Flash only", with
    no quality problem recorded. `test_intake_models` only enforced that choice.
- **Change:**
  - `app/llm/model_policy.py`: `FALLBACK_MODELS = ("nvidia/openai/gpt-oss-20b",)` with an
    alias, and `minimax` joins `RETIRED_MODEL_MARKERS`.
  - The policy now also guarantees the NVIDIA provider row for the fallback. It matches the
    live row, so there is no churn. `_ensure_model_row` serves both providers.
  - `nvidia/*` refs keep NVIDIA auth and key rotation. The rule "never pin `nvidia:keyN` on
    `openai/*`" is unaffected, since the ref starts with `nvidia/`.
  - The same model replaces MiniMax in the API model catalog, the unused NVIDIA branch of
    `app/memory/vector.py`, and `ops/nvidia_key_probe.py`. One `config.py` comment is
    updated.
- **Applied:**
  - `settings.json` (backup in `data/backups/`): `task_registry.intake_model_fallbacks`
    changed on one line. `settings.example.json` got the same change.
  - `openclaw.json` (backup `openclaw.json.pre-gpt-oss-fallback.20260928T141934Z`): the dry
    run on a copy matched the live apply. Fallbacks, alias and allow list moved to
    gpt-oss-20b; the MiniMax alias and model row were dropped.
  - The gateway hot-reloaded the config, and Slack reconnected in 1 s.
- **Docs:** CONCEPT_TREE §4, §8 and §10, both `rmp-architecture.mdc` copies (identical),
  README, ARCHITECTURE and the patcher's messages. Historical records are unchanged.
- **Tests:** `test_model_policy` checks the new chain, the pinning rule for the NVIDIA-hosted
  fallback, and that MiniMax is dropped while the fallback row is ensured. `test_intake_models`
  now asserts gpt-oss-20b in the chain and no MiniMax. Full suite: 492 passed, 3 skipped.
- **Live check:** one intake-style turn on `nvidia/openai/gpt-oss-20b` through
  `_dispatch_openclaw_session`. The gateway logged `status=200`, first byte in 865 ms, and
  RMP read the JSON reply in 6.2 s. The last MiniMax 410 was at 13:14Z. The reloader
  restarted the API and worker at 14:18:48Z, and runtime sync reports no stale services.

## Step 9 — Evaluator fallback (2026-09-28)

- **Finding:** `_execute_on_internal_session` made one turn on `rmp_verify_{task}` with no
  model. Any gateway error, quota timeout or unparseable verdict came back as `Error:`,
  which fails closed into rework. Intake already walks its model chain.
- **Change (`app/activities/openclaw_activities.py`):**
  - The evaluator walks `drop_unwired_openai([SUBAGENT_MODEL, *FALLBACK_MODELS])`: gpt-5-nano,
    then `nvidia/openai/gpt-oss-20b` (MiniMax per the plan, replaced per Kirill's decision).
    Each turn passes an explicit `model`.
  - Like intake, each fallback gets its own session (`rmp_verify_{task}_fb1`), so no sticky
    model override or half-written failed turn carries over.
  - It moves on after `OpenClawError`, a `TimeoutError`, or a reply that
    `parse_evaluator_response` marks `parse_error`. All attempts share the Step 4 activity
    deadline. If every model fails, it returns the last error, so the existing fail-closed
    verdict is unchanged.
- **Tests (`tests/test_llm_orchestration.py`):**
  - 4 new: fallback after a gateway error (asserting session keys and explicit models);
    fallback after an unparseable verdict; a good first verdict skips the fallback; every
    model failing returns the last error as `parse_error`.
  - The Step 4 deadline test now expects the deadline on both attempts. The OpenAI key check
    is pinned in these tests so CI behaves the same.
  - Full suite: 496 passed, 3 skipped.
- **Live check:** one evaluator-style turn through `_execute_on_internal_session` returned
  `accept` (`parse_error` False) in 7.1 s. The gateway logged gpt-5-nano `status=200` with
  the first byte in 3.1 s.
- **Step 6 follow-up from the same log:** there has been no OpenAI idle cut since the 12:58Z
  patch. All later OpenAI calls returned 200, including first bytes of 5.2–9.8 s that the
  old rule would have cut.
