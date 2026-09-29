# Invariant alerts

The canary sentinel (`rmp-canary-sentinel.timer`, every 30 min) runs the checks in `app/production/invariants.py` and DMs Kirill **"⚠️ RMP invariant broken"** when one fails. `make readiness` shows every check, warnings included. Run them now: `venv/bin/python -m app.production.canary_sentinel`.

A violation counts for 24 h, then ages out: after the fix, the check keeps failing until then. That is the record, not a new failure. The same set of failing checks alerts at most every 4 h, and only when the details change.

Timestamps in `rmp_db` are naive UTC. Compare with `now() at time zone 'utc'`, not `now()` (the server runs Europe/Berlin).

## judged_deliveries (fail)

A user task completed without an `evaluator.accept`: a reply reached Kirill unjudged.

1. `select event_type, occurred_at from events where entity_id = '<task>' order by occurred_at;`
2. Find the path that completed it (`reconciler.*` events, a workflow exit that skipped `_judge_and_deliver`). Every path to `completed` must pass an accept. Fix the path; never add an accept by hand.

## attached_messages (fail)

A task ended without answering a message attached to it (`intake.attach`) and without resubmitting it (`task.messages_resubmitted`).

1. The message is in `task_messages`: `select role, left(content, 80), created_at from task_messages where task_id = '<task>' order by created_at;`
2. Tell Kirill it was missed, or send it through intake again (`POST /tasks` with the text and his session key).
3. Find the workflow exit that skipped `_resubmit_leftovers` (`app/workflows/`).

## slack_delivery (fail)

Slack refused a delivery permanently.

1. `select entity_id, event_payload from events where event_type = 'slack.delivery_failed' order by occurred_at desc limit 5;`
2. `invalid_auth` / `token_revoked`: the bot token. `channel_not_found` / `user_not_found`: the session's Slack user id.
3. The accepted text is the last assistant turn of the task's `agent:main:rmp_task_<task>` session; send it once the cause is fixed.

## memory_hygiene (fail)

Canary, heartbeat or system traces reached user or procedural memory or the task registry.

1. `venv/bin/python -m ops.purge_internal_memory` (dry run) lists them.
2. Find the write that let them in (promotion, the registry indexer, an Aura memory tool), fix it, then run with `--apply` (it backs up every row it deletes).

## vector_sync (fail: outbox; warn: drift)

- **Outbox overdue** (> 15 min): the API's drain is not running. `journalctl -u rmp-api | grep -i "vector outbox"`, then `make restart-rmp`.
- **Outbox failing** (≥ 5 attempts): `select kind, ref_id, attempts, last_error from vector_outbox where done_at is null order by attempts desc limit 10;` Usually the embedder key or Qdrant (`systemctl status rmp-qdrant`).
- **Drift** (warn): the daily reconcile repairs it. Now: `venv/bin/python -m ops.reconcile_vectors --apply`.

Recall keeps working meanwhile: when the index does not answer, it reads Postgres full text.

## orphan_recoveries (warn)

A run died after Aura answered (worker crash or restart). The reconciler restarted it so the evaluator judges the draft before anything is sent. One after a restart is expected. Repeated ones mean the worker is crashing: `journalctl -u rmp-worker`.
