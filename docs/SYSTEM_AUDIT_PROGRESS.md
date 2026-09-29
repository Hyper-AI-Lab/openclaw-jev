# System audit and hardening — progress log

Append-only. One entry per plan step, newest at the bottom. Each entry states what was done, the evidence, the files changed and how it was verified.

---

## Step 1 — Switch to gpt-6-luna (Kirill's choice: finish and deploy before the audit)

**Date:** 2026-09-29 (17:09–17:30 CEST)  
**Decisions (Kirill):** gpt-6-luna everywhere; thinking `max` for Aura's task work, `medium` for intake, the Process Evaluator and canaries; only OpenAI calls at `max` get 120 s for the first byte and 30 s between chunks.

**Findings before the change (live probes on this account):**

- `gpt-6-luna` is listed for the key. Chat Completions rejects function tools with any reasoning effort (HTTP 400) and has no `max` (efforts there: none…xhigh). The Responses API takes tools at `max`.
- Real intake prompt, direct API: none 10.4 s, low 6.9 s, medium 5.9 s, high 9.0 s, xhigh 14.5 s, max 35.6 s (4,311 reasoning tokens, $0.0027). Same decision from medium up.
- Streaming at max: one run sent nothing for 69 s (total 88 s); another streamed at once with 4.5–5.0 s gaps. Medium: 4 s, gaps ≤ 2.5 s.

**What changed:**

- `app/llm/model_policy.py`: primary and subagent model `openai/gpt-6-luna`; OpenAI provider `api: openai-responses`; model row with cost, `contextWindow` 272k (the long-context price tier starts above 272k input tokens), `thinkingLevelMap` {xhigh, max} and `compat.supportedReasoningEfforts`; `thinkingDefault` and alias default `medium`; `gpt-5-nano` retired (alias and row dropped; OpenAI shuts it down 2026-12-11).
- `app/activities/openclaw_activities.py`: `_dispatch_openclaw_session(thinking=…)` sends the hook's `thinking`; `send_to_openclaw` passes `max` for user tasks and `medium` for canary/internal tasks. `app/workflows/generic_task.py`: rework dispatch carries `task_type` and `tags`, so a canary rework stays at medium.
- `patch_openclaw.sh` Patch 6d (`RMP_OPENAI_MAX_EFFORT_120S`): OpenAI calls with `options.reasoning === "max"` wait up to 120 s for the first byte (idle wrapper and provider first-event guard) and 30 s between chunks.
- `patch_openclaw.sh` Patch 6e (`RMP_GPT6_THINKING_BACKPORT`): the first live max run showed `thinking_level_change: high`. OpenClaw 2026.9.1's OpenAI thinking policy offers `max` only to `gpt-5.6*`. Backported 2026.9.6's GPT-6 branch (levels from the declared efforts); it skips itself on releases that ship `OPENAI_GPT_6_MODEL_IDS`.
- Live config: `openclaw.json` via `apply_openclaw_policy`; `settings.json` `intake_model` → `openai/gpt-6-luna`. Backups: `/root/.openclaw/{openclaw,settings}.json.bak-luna-20260929-170939`.
- Docs and rules: both `rmp-architecture.mdc` copies (identical), `CONCEPT_TREE.md` (MUST, §8, contradiction register), `ARCHITECTURE.md`, `README.md`, `settings.example.json`, readiness text, verify/upgrade scripts.

**Verification:**

- Offline: 500 Python tests pass (the one live-settings test passes after rollout), 11 node tests pass. New tests: thinking per task kind; rework carries tags; policy row and retirement. One-off undefined-name check on the edited functions: 0 (it caught an earlier `payload` scope slip before commit).
- Candidate config validated with `OPENCLAW_CONFIG_PATH=… openclaw config validate` before going live; live config valid; `ops/verify_openclaw_patch.sh` passed (primary and subagents gpt-6-luna, both new markers).
- Patch 6e offline import test: gpt-6-luna gets off…max with default medium; gpt-5-nano and gpt-5.6-terra unchanged; idempotent.
- Live max-effort Aura turn (scratch `rmp_task_lunacheck_*`, `deliver: false`): `thinking_level_change: max`, `read` tool call, correct answer, 27 s, no idle cuts.
- Same intake prompt through OpenClaw: high 315 reasoning tokens, max 1,001. The stored OpenAI responses report `reasoning.effort` `high` and `max` respectively.
- Intake LLM-path canary: PASS, 18 s (target 45 s), gpt-6-luna at medium. Health canary: `CANARY OK`, task session gpt-6-luna at medium.
- Process Evaluator prompt on gpt-6-luna at medium: correct draft → accept (6.6 s); off-topic draft → rework with a concrete `command_to_aura` (6.5 s).

**Incidents and notes:**

- The idle-aware code watcher reloaded `rmp-api`/`rmp-worker` about 3 minutes after the first `model_policy.py` edit, before OpenClaw allowed gpt-6-luna. Edits moved to a git worktree and the live tree was restored at 16:09:43. Only one run fell in the window, the hourly canary, which ran on gpt-5-nano and passed. Task runs follow `openclaw.json`'s primary, so only evaluator calls could have been affected.
- The Responses API path sends `store: true`: OpenAI keeps response objects (prompts and replies) for its retention window, which Chat Completions did not. OpenClaw uses the stored reasoning items across turns. Open item for the audit plan.
- OpenClaw 2026.9.6 (2026-09-23) supports GPT-6 natively. Upgrading would retire Patch 6e. Open item for the audit plan.
- Scratch sessions left in the OpenClaw store: `rmp_task_lunacheck_*`, `rmp_task_lunaeffort_*`, `rmp_verify_lunacheck_*` (no RMP task rows, `deliver: false`).
