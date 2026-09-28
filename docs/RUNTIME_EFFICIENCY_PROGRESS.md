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
