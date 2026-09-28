# Jev decisions for Aura

Model `jev-1.13.0` (pinned), endpoint `https://api.typesafe.ai/v1/systemone`.
Two consumers, each `off` / `shadow` / `enforce` under `settings.json` → `jev`.
Both default to `off`. Settings are read on every call, so a mode change applies
without a restart. No OpenClaw upgrade, database migration, new service, inbound
port or dependency is involved.

## What each consumer does

**Intake (`intake_mode`).** Runs after the deterministic gates, in
`classify_task_intake` on the worker and in the API fallback
`classify_task_intake_deterministic`. One request with up to seven Choice
questions (see `app/decisions/intake.py`). In `enforce`, an accepted answer
becomes the same `llm_result` the intake LLM returns and goes through
`apply_intake_policy` unchanged, so the OpenClaw `rmp_intake_*` turn and its LLM
quota slot are skipped. Everything else keeps the existing path. Shadow runs and
abstentions store the proposal in `task_intake_decisions.llm_raw.jev`.

An answer is accepted only when:

- `relation` is not `unclear` and reaches `intake_min_confidence` (0.85), and so does `execution_mode`;
- a running-task decision has relation, target and action at `intake_attach_min_confidence` (0.92);
- a finished-work follow-up names a listed finished task;
- every required label is the provider's top estimate.

`catalog_hint` is kept only for `structured_work` at 0.9 or above. `web_intent`
is kept at 0.85 or above. Canary, system and heartbeat tags never call Jev.

**Memory promotion (`promotion_mode`).** Runs after `validate_fact` and before
semantic or pinned writes. Three Choices per fact (support, durability, user
scope), each at 0.95 confidence and probability. `enforce` holds the rest.
Procedural copies are unchanged.

## Evaluation

Structure checks, no model call:

```bash
cd /root/.openclaw/rmp
./venv/bin/python -m ops.jev_eval            # memory promotion fixtures
./venv/bin/python -m ops.jev_eval --intake   # intake fixtures
```

Replay cases from past DMs (read-only on the database). The file holds Kirill's
messages, lives under git-ignored `data/`, and must never be committed. Labels
in `expect` are drafts to review.

```bash
./venv/bin/python -m ops.jev_replay_export   # writes data/jev_intake_replay.jsonl
```

Paid live run, no database or Slack writes:

```bash
TYPESAFE_API_KEY="$(sed -n 's/^TYPESAFE_API_KEY=//p' /etc/openclaw/openclaw.env)" \
  ./venv/bin/python -m ops.jev_eval --intake --live \
  --dataset tests/fixtures/jev_intake_eval.jsonl --dataset data/jev_intake_replay.jsonl \
  --output data/jev-intake-live.json
```

Intake may move to `enforce` only when the report's `gate.passed` is true:

- zero harmful errors (an accepted attach, wait or rebuild on the wrong task, or a wrong catalog template);
- accuracy on accepted decisions of at least 0.9;
- coverage of at least 0.5;
- p95 latency below 1500 ms.

Promotion stays in `shadow` until its own evaluation shows zero false global
promotions.

## Rollout on this host

VNC is only the admin desktop. The consumers run inside the `rmp-api` and
`rmp-worker` systemd units.

1. Record `git rev-parse HEAD` and `openclaw --version`.
2. Add `TYPESAFE_API_KEY=...` to `/etc/openclaw/openclaw.env`. Both units load
   that file; keep the key out of JSON, git and command arguments.
3. Run the live evaluation above.
4. Set `jev.intake_mode` and `jev.promotion_mode` to `shadow` in `settings.json`.
5. Restart only when no user task is active (`count_active_user_tasks_sync() == 0`):
   `systemctl restart rmp-api rmp-worker`, then `make production-check` and `make canary`.
6. Compare shadow proposals with the decisions that ran:

```bash
sudo -u postgres psql -d rmp_db -c "select created_at, decision,
  llm_raw::jsonb->'jev'->'proposal'->>'decision' as jev_decision,
  llm_raw::jsonb->'jev'->>'accepted' as accepted, llm_raw::jsonb->'jev'->>'latency_ms' as ms
  from task_intake_decisions where llm_raw::jsonb ? 'jev' order by created_at desc limit 20;"
```

7. When the gate passes, set `jev.intake_mode` to `enforce`. Send one test DM and
   check that its row has `llm_raw.decision_source = jev`.

The intake LLM path stays covered while Jev enforces: `ops/canary_intake_latency.sh`
(part of `make production-check`) calls `POST /tasks/intake/preview?bypass_jev=true`
and must see an LLM decision within 45 s. Only the preview accepts the flag.

Rollback: set the mode to `off` (takes effect on the next call), or set
`AURA_JEV_MODE=off` in the service environment and restart when idle.

## Limits

- Deadline, including queueing and the full body read: 3 s (configurable 0.05–5 s).
- Per process: 2 concurrent requests and 60 requests per minute.
- Request / response size: 24,000 / 65,536 bytes; oversized input is rejected, never truncated.
- Questions: at most 32; intake uses up to 7; promotion uses 3 per fact for up to 8 facts.
- Cache: 128 successful results for 30 s, in RAM only.
- Circuit: 30 s after three consecutive transport or contract errors; 429/529 wait at least 30 s and honor `Retry-After`.
- No hot-path retries; cancellation propagates without a fallback call.

## Data handling

Intake sends the DM text, the last four same-session turns, up to four running
task goals, five finished-task summaries and three memory snippets. Aliases
(`R1`, `F1`) replace task ids. Promotion sends the episode and candidate facts.
Recognized secret patterns are redacted; this is not full PII filtering.
TypeSafe states it does not train on requests; zero data retention is an
enterprise option. Logs keep request hashes, rubric ids, status, latency and
typed answers, never raw text or credentials.

## Known gaps

- User tasks since September have no `task_registry_entries` rows, so
  finished-work evidence for recent follow-ups is missing for the LLM analyst and
  for Jev alike.
- DMs where Jev abstains still take the slot-gated OpenClaw intake turn.
- The original PostgreSQL memory read path does not filter `valid_to`.

References: [API](https://docs.typesafe.ai/api),
[limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13),
[confidence](https://docs.typesafe.ai/confidence).
