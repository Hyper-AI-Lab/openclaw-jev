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
