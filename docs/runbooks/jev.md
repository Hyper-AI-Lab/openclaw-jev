# Jev pilot for Aura

Base: `09502006211d6ba6442fe8c5c051b5b0259dbd1c` (2026-09-27).
The adapter pins the direct TypeSafe endpoint and `jev-1.13.0`.
Both consumers default to **off**. No OpenClaw upgrade, database migration,
new service, inbound port, or new production dependency is required.

## Behavior

| Consumer | Placement | Enforce behavior | Failure behavior |
| --- | --- | --- | --- |
| Evidence rerank | After hybrid evidence-pack assembly | Stable relevance ordering; retains every row, citation, liveness flag and RRF score | Original order |
| Global promotion review | Between extraction and existing write gates | Requires support, durability and explicit user-level scope | Holds semantic/pinned promotion |

The reranker applies to the intake evidence pack, not all process-memory reads.
It cannot assign tasks or workflows. The promotion guard covers semantic and
pinned copies only. Existing procedural copies remain outside this pilot.
Held candidates are not deleted, but there is no new retry queue. Existing
source records and Temporal activity history support investigation within their
retention windows.

Shadow retains original behavior, including original promotion risks, while
recording proposed decisions. Shadow sends evidence to TypeSafe and incurs
charges. It is an observation mode, not a protection mode.

Promotion review checks entailment against supplied prose; it does not prove
that an agent's claimed outcome occurred. Existing evidence requirements,
the independent completion evaluator, and Slack delivery ownership still apply.

## Verification

Use Aura's Python environment with the repository requirements, from its root:

```bash
python -m pytest -q tests/test_jev.py tests/test_hybrid_retriever.py tests/test_promotion.py tests/test_memory_policy.py tests/test_memory_writes.py tests/test_intake_bounded_context.py
python -m ops.jev_eval
```

The second command validates ten synthetic labeled cases without a model call.
Mock tests and synthetic labels do not establish Jev accuracy. Run the full
pytest suite before merging. A test host using a SOCKS proxy may need optional
`socksio` for existing embedding-client tests.

## Remote VNC host rollout

VNC is the administrative interface. Run the integration in Aura's API and
Temporal worker processes on the remote machine, independent of the desktop
session. It needs outbound HTTPS to `api.typesafe.ai:443` and TypeSafe credits;
no GPU is required. A VNC terminal export does not change an already-running
systemd service's environment.

1. Record `git rev-parse HEAD` and `openclaw --version`. Back up settings and
   the database with the existing Aura runbook. Apply on an isolated branch,
   first checking `git apply --check /path/to/aura-jev-integration.patch`.
2. Run tests with both modes off. This change needs no OpenClaw dist patch or
   upgrade. Do not run `openclaw onboard`.
3. Supply `TYPESAFE_API_KEY` through the existing protected environment source
   for the Aura API and worker. Keep it out of JSON, command arguments and git.
   Only this key is read; chat and embedding keys are not reused.
4. Merge the `jev` block in `settings.example.json` into actual settings; start
   with both modes `shadow`. Preserve all other settings.
5. In an idle maintenance window, restart the actual API/worker units using the
   existing runbook, then check readiness and canaries. Do not restart a busy
   worker merely to enable this pilot.
6. In the configured service environment, run the explicit paid evaluation:

```bash
python -m ops.jev_eval --live --output data/jev-live-synthetic.json
```

The default dataset is synthetic. The command performs no database, memory,
workflow or Slack mutations. Reports include unavailable calls, served model,
latency, input-token cost estimate, false promotions, false holds and NDCG@3
against original input order. Costs of failed/unreported calls may be missing.

7. Label representative authorized Aura cases in the same JSONL format. Keep
   a time-separated holdout untouched while tuning. Compare baseline, Jev,
   and deterministic fixes alone. Include actual deployment languages and
   adversarial text. The default CLI limit is 20 cases; raise `--max-cases`
   explicitly for a reviewed batch.
8. Enable consumers separately after measurement. Proposed trial criteria:
   no false global promotions in at least 300 accepted holdout decisions;
   report false holds and coverage as well. Zero errors in 300 independent,
   representative accepted decisions gives approximately a 1% one-sided 95%
   binomial upper error bound, not a guarantee. Require improved NDCG@3 without
   worse task relations and measure full server-side p95 and total bills.
   These criteria have not yet been demonstrated by this implementation.

## Limits and data handling

| Control | Default / fixed bound |
| --- | --- |
| Deadline, including queue and full body read | 1 second; configurable 0.05–5 seconds |
| Concurrent requests per process | 2 |
| Requests per rolling minute per process | 60; configurable 1–120 |
| Complete request / response | 24,000 / 65,536 bytes |
| Questions | 32 maximum; promotion uses three per candidate |
| Shortlists | 12 ranking candidates / 8 promotion candidates |
| Successful response cache | 128 entries, 30-second TTL, RAM only |
| Circuit | 30 seconds after three consecutive transport/contract errors |
| HTTP 429 / 529 | At least 30 seconds; honors a longer Retry-After |
| Ranking gate | All answers confident at 0.80; any unknown preserves baseline |
| Promotion gate | Each required Choice confidence AND selected probability >=0.95 |

Thresholds are provisional. Limits and caches are per process, so a multiworker
deployment multiplies aggregate allowances. Oversized inputs are rejected,
not truncated. No hot-path retries occur. Cancellation propagates without a
replacement model call. These consumers add model calls; the reranker retains
all evidence rows and does not reduce prompt-token volume.

Only shortlisted query/snippet/outcome text or episode/candidate text is sent.
The component collects no screenshots, ambient conversation, filesystem files
or whole memory database. IDs and credentials are not evidence. Existing Aura
secret patterns redact recognized secrets; they are not comprehensive PII
filtering or a prompt-injection defense. Evidence must be suitable for the
provider. Jev judgments never grant effect permission.

Logs contain request hashes, model/rubric IDs, status, timing, order indices
and typed promotion judgments. Raw prompts, response bodies, credentials and
unknown response fields are excluded. Cache identity includes scope, request,
rubric and credential. Cached usage describes the original request: exclude
`cache_hit=true` from new billable-token totals. The evaluation CLI disables
this cache.

The client ignores HTTP proxy environment variables and refuses redirects.
If the server requires a proxy, an explicitly approved transport needs testing
before activation. Gateways and local models are not enabled in this pilot;
provider changes require contract tests and fresh threshold calibration.

## Rollback and remaining work

Set both modes to `off`; settings are checked on each call. Alternatively set
`AURA_JEV_MODE=off` in the service environment and safely restart. Off restores
old behavior, without undoing existing memories or automatically promoting held
candidates. Invalid Jev configuration also disables the consumers with a bounded
warning. This restores the old promotion policy rather than adding protection.

A separate lifecycle audit is needed: the original PostgreSQL memory read query
does not filter `valid_to`, and graph/vector deletion semantics need checking
together. This pilot does not make a partial lifecycle fix. Jev cannot repair
embedding outages or reliably order dates. The README's old NVIDIA outage note
also differs from the current OpenAI embedding defaults; inspect live readiness.

Future candidates: a typed intake cascade retaining the original LLM fallback;
source-receipt support checks before completion review; web-backend selection
from textual DOM/tool results. Keep network I/O in activities or API operations,
never Temporal workflow code. Jev 1.13 is text-only, so VNC screenshots still
need the existing vision path or separately tested text extraction.

References: [API](https://docs.typesafe.ai/api),
[limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13),
[OpenClaw role](https://docs.openclaw.ai/concepts/decision-models).
