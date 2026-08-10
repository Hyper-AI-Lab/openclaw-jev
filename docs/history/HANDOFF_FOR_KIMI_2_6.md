# OpenClaw RMP Handoff for KIMI 2.6

Last updated: 2026-05-06

This document is a technical handoff for continuing development of the OpenClaw Reliability and Memory Plane (RMP). It summarizes the original user requirements, what has been built, how it connects to OpenClaw, Slack, Moltbook/Moltbot-related workspace automation, where the relevant files and logs live, what is currently working, and what should be fixed next.

Important security note: this document intentionally does not include API keys, Slack tokens, gateway tokens, or Moltbook credentials. Some source/config files on disk do contain secrets. Read them only when necessary and do not paste secret values into chat, logs, commits, or documentation.

## 1. Original Goal

The user wanted to upgrade OpenClaw because OpenClaw relied too heavily on the LLM as the control plane. This caused memory loss, unfinished tasks, duplicate or drifting work, and unstable task execution when the model quality was not ideal.

The initial request is in:

- `/root/request/request.txt`
- `/root/request/openclaw_reliability_memory_upgrade_report.md`
- `/root/request/research2.md`
- `/root/request/research3.md`

The design target from these inputs:

- Build a standalone Reliability and Memory Plane around OpenClaw.
- Treat OpenClaw and the LLM as execution tools, not as the source of truth for task state.
- Intercept every user message before the main agent handles it.
- Create a durable task and workflow before execution starts.
- Dispatch the task to an isolated OpenClaw internal session.
- Require the agent to append structured task-state JSON.
- Independently verify answer quality before delivering to the user.
- Retry or continue when the task is incomplete, failed, or materially wrong.
- Let the user stop a task at any point.
- Ask the user whether to continue after 10 failed/incomplete cycles.
- Show tasks and monitoring in a dashboard at `http://localhost:8000/`.
- Keep the RMP mostly independent from OpenClaw updates, with a patch script for necessary OpenClaw internals.
- Disable model fallback so only the selected model is used.
- Let the dashboard switch provider/model/API key.
- Monitor cron/long-running processes as tasks.
- Route memory by task/process scope in future development.

The central architecture requested by the user is:

```text
User request
  -> RMP interception
  -> durable task
  -> Temporal workflow/process
  -> bounded OpenClaw agent execution
  -> task-state parsing
  -> independent quality check
  -> retry/continue/stop/final delivery
```

## 2. High-Level Current Architecture

The deployed implementation is a sidecar RMP built around:

- OpenClaw gateway and Slack bot as the user-facing agent/channel layer.
- A custom OpenClaw plugin (`rmp_adapter`) for message interception.
- FastAPI for task API and dashboard.
- Temporal for durable workflows.
- PostgreSQL for task records and future process/memory/event records.
- Internal OpenClaw sessions for actual agent execution and quality verification.
- Direct Slack API delivery from RMP for clean final answers.

Current flow for a Slack DM:

1. User sends a Slack DM to Aura/OpenClaw.
2. OpenClaw receives it and prepares to write the user message into the main session.
3. `/root/.openclaw/plugins/rmp_adapter/index.js` handles `before_message_write`.
4. The plugin extracts the real Slack intent from text like `Slack DM from ...: ...`.
5. The plugin calls `POST http://127.0.0.1:8000/tasks`.
6. The plugin blocks the original message so the main agent should not answer directly.
7. FastAPI creates a row in `tasks`.
8. FastAPI starts Temporal workflow `workflow-<task_id>` on task queue `openclaw-tasks`.
9. The Temporal worker runs `GenericTaskWorkflow`.
10. The workflow sends an execution prompt to OpenClaw via `/hooks/agent` using an internal session key:
    - `agent:main:rmp_task_<task_id>`
11. The workflow polls the generated OpenClaw `.jsonl` session file for the assistant response.
12. The workflow parses the `{"task_status": ...}` JSON block from the response.
13. If the agent says `completed`, the workflow sends the clean answer to a separate verification session:
    - `agent:main:rmp_verify_<task_id>`
14. The verifier returns `{"quality": "pass"}` or `{"quality": "fail"}`.
15. If quality passes, the workflow marks the task completed and sends the clean answer to the user through Slack.
16. If quality fails materially, the workflow retries with feedback.
17. If status is pending, the workflow retries and may send intermediate updates depending on dashboard settings.
18. If 10 attempts are reached, the workflow marks `pending_user_input` and asks the user whether to continue or stop.
19. If the user sends stop/cancel/abort/halt, the plugin signals the workflow and the task is marked stopped.

## 3. Important File Map

### RMP Core

- `/root/.openclaw/rmp/app/api/server.py`
  - FastAPI app.
  - Dashboard endpoint `/`.
  - JSON dashboard endpoint `/api/dashboard-data`.
  - Task creation endpoint `POST /tasks`.
  - Task status endpoint `GET /tasks/{task_id}`.
  - Task signal endpoint `POST /tasks/{task_id}/signal`.
  - Active task lookup endpoints:
    - `GET /sessions/{session_key}/active_task`
    - `GET /sessions/{session_key}/active_user_task`
  - LLM config endpoint `POST /config`.
  - Settings endpoints `GET/POST /settings`.
  - Dynamic model catalog via `openclaw --no-color models list --all --json`.
  - Reads and writes:
    - `/root/.openclaw/openclaw.json`
    - `/root/.openclaw/agents/main/agent/auth-profiles.json`
    - `/root/.openclaw/rmp/settings.json`

- `/root/.openclaw/rmp/app/api/templates/dashboard.html`
  - Dark web dashboard.
  - Shows total/running/completed/failed tasks.
  - Shows active model and API-key hint.
  - Has provider/model dropdowns.
  - Supports custom model entry.
  - Lets user update provider/model/API key.
  - Has toggle: "Send intermediate task updates to user".
  - Polls `/api/dashboard-data`.

- `/root/.openclaw/rmp/app/workflows/generic_task.py`
  - Temporal workflow implementation.
  - Main class: `GenericTaskWorkflow`.
  - Signal: `user_input`.
  - Implements retry loop, task status parsing, quality check, Slack delivery, stop handling, and 10-cycle user confirmation.
  - Helper behavior:
    - `strip_json_eval()` removes trailing task-status JSON from user-facing replies.
    - `is_heartbeat_request()` and `is_heartbeat_ack()` suppress plain heartbeat acknowledgements.
    - `is_material_quality_failure()` tries to retry only for material factual/requirement errors.
  - Known limitation: current completion and quality policies are still heuristic and should be hardened with evidence-based checks.

- `/root/.openclaw/rmp/app/activities/openclaw_activities.py`
  - Temporal activities for calling OpenClaw and Slack.
  - `send_to_openclaw()` calls OpenClaw `/hooks/agent` with `deliver: false`, then polls internal OpenClaw session JSONL.
  - `parse_agent_evaluation()` extracts `{"task_status": ...}`.
  - `verify_response_quality()` runs independent LLM-based review in an internal verification session.
  - `notify_slack_user()` posts directly to the user's Slack DM using the configured Slack bot token.
  - `_get_slack_user_id()` maps OpenClaw session origin to Slack user ID. It handles `slack:`, `user:`, and `slack:user:` prefixes.
  - Known issue: user-facing strings like `[[reply_to_current]]` can leak from OpenClaw output and should be stripped here or before delivery.

- `/root/.openclaw/rmp/app/activities/db_activities.py`
  - Currently only `update_task_status()`.
  - Updates task status in Postgres.

- `/root/.openclaw/rmp/app/db/models.py`
  - SQLAlchemy models:
    - `Task`
    - `ProcessRun`
    - `Step`
    - `Observation`
    - `Event`
  - At present, `Task` is actively used. `ProcessRun`, `Step`, `Observation`, and `Event` exist as schema foundations but are not fully integrated into workflow logic yet.

- `/root/.openclaw/rmp/app/db/database.py`
  - Async SQLAlchemy engine/session setup.
  - Default DB URL:
    - `postgresql+asyncpg://rmp:rmp_password@localhost/rmp_db`

- `/root/.openclaw/rmp/worker.py`
  - Temporal worker.
  - Connects to `localhost:7233`.
  - Task queue: `openclaw-tasks`.
  - Registers `GenericTaskWorkflow` and all RMP activities.

- `/root/.openclaw/rmp/settings.json`
  - Current setting:
    - `intermediate_updates: true`
  - Dashboard toggle controls this.

- `/root/.openclaw/rmp/patch_openclaw.sh`
  - Re-applies critical patches after an OpenClaw update.
  - Target: `/usr/lib/node_modules/openclaw/dist`.
  - Patches:
    - hook persistence for `before_message_write`
    - disable model fallback
    - suppress subagent announce flows for `rmp_task_` and `rmp_verify_` sessions
  - Must be rerun after reinstall/update of OpenClaw.

### OpenClaw Integration

- `/root/.openclaw/plugins/rmp_adapter/index.js`
  - Main OpenClaw plugin.
  - Registers two tools:
    - `rmp_task_create`
    - `rmp_task_status`
  - Registers the critical `before_message_write` hook with `api.on(...)`.
  - Why `api.on`: `before_message_write` is a typed hook; earlier usage of `api.registerHook()` did not reliably intercept messages.
  - Responsibilities:
    - Extract user intent from Slack DM text.
    - Detect cron/heartbeat/system messages.
    - Create RMP task via `POST /tasks`.
    - Block the original message so the main OpenClaw agent does not answer directly.
    - Signal active tasks for stop/cancel/abort.
    - Signal active tasks with follow-up user input instead of spawning duplicates.
    - Block internal RMP messages and hook auto-delivery messages.
  - Known limitation: duplicate delivery can still happen in some cases, likely through an OpenClaw delivery path not fully blocked or because the internal response includes OpenClaw reply tags.

- `/root/.openclaw/openclaw.json`
  - Main OpenClaw config.
  - Current important values:
    - primary model: `openai/gpt-5.4`
    - heartbeat: `4h`
    - gateway port: `18789`
    - Slack enabled in socket mode
    - plugins enabled: `rmp_adapter`, `slack`
    - hooks enabled
    - `hooks.allowRequestSessionKey: true`
  - Contains secrets. Do not paste raw content into logs or docs.

- `/root/.openclaw/agents/main/agent/auth-profiles.json`
  - Stores provider API-key profiles.
  - Dashboard updates this.
  - The old Google model key was removed from active auth profiles during prior work.
  - Contains secrets.

- `/root/.openclaw/agents/main/sessions/sessions.json`
  - OpenClaw session registry.
  - Used to map session keys to actual JSONL session IDs.
  - Contains records for:
    - `agent:main:main`
    - `agent:main:rmp_task_<task_id>`
    - `agent:main:rmp_verify_<task_id>`

- `/root/.openclaw/agents/main/sessions/*.jsonl`
  - OpenClaw conversation/session history.
  - RMP polls these files to capture agent and verifier outputs.
  - Useful for debugging duplicate delivery, parsing failures, and quality-check decisions.

### Workspace, Moltbook, Moltbot, and Long-Running Automation

The user refers to Moltbook/Moltbot-related work as part of the broader OpenClaw/Aura environment. There are Moltbook scanner scripts and memory/task notes in the OpenClaw workspace.

Relevant files:

- `/root/.openclaw/workspace/.env.moltbook`
  - Contains Moltbook API configuration and profile data.
  - Contains secrets. Do not paste.

- `/root/.openclaw/workspace/moltbook_1hr_max.js`
- `/root/.openclaw/workspace/moltbook_10m_scan.js`
- `/root/.openclaw/workspace/moltbook_60m_scan.js`
- `/root/.openclaw/workspace/moltbook_60m_fast_scan.js`
- `/root/.openclaw/workspace/moltbook_60m_max_speed.js`
- `/root/.openclaw/workspace/moltbook_180m_scan.js`
  - Scripts that read `.env.moltbook` and call the Moltbook API feed.
  - Previously had hardcoded Google API key references scrubbed/replaced.

- `/root/.openclaw/workspace/moltbook_research_log.md`
  - Logs/analysis from Moltbook scanning.

- `/root/.openclaw/workspace/moltbook_seen_posts.json`
  - Seen-post state for scanner scripts.

- `/root/.openclaw/workspace/tasks.md`
  - Workspace task notes, including Moltbook scanner checks.

- `/root/.openclaw/workspace/processes.md`
  - Workspace process notes.
  - Includes `4hr-Scraper` marked inactive / definition updated.

- `/root/.openclaw/workspace/MEMORY.md`
  - OpenClaw memory notes. Contains Moltbook scanner notes and other continuity facts.

Current RMP state does not yet fully model these scanner scripts as first-class `ProcessRun` rows. That is a key remaining development target: any long-running scanner, cron job, heartbeat job, or external automation should be represented as a durable task/process in RMP.

## 4. Services, Ports, and Runtime

Expected components:

- Temporal server:
  - Port `7233`
  - UI port `8233`
  - Command usually: `temporal server start-dev --ip 0.0.0.0`

- RMP API:
  - Port `8000`
  - Expected command:
    - `cd /root/.openclaw/rmp && /root/.openclaw/rmp/venv/bin/uvicorn app.api.server:app --host 0.0.0.0 --port 8000`

- RMP worker:
  - Expected command:
    - `cd /root/.openclaw/rmp && /root/.openclaw/rmp/venv/bin/python worker.py`

- OpenClaw gateway:
  - Port `18789`
  - Expected command:
    - `openclaw gateway --force`

- PostgreSQL:
  - Local port `5432`
  - Database: `rmp_db`
  - User: `rmp`

Important current-state observation on 2026-05-06:

- PostgreSQL is listening on `127.0.0.1:5432`.
- Temporal is running and listening on `7233` and `8233`.
- The RMP API was not responding on `127.0.0.1:8000` at handoff time.
- `worker.py` was not running at handoff time.
- `openclaw-gateway` was not running/listening on `18789` at handoff time.
- `systemctl` did not find `rmp-worker.service`, `rmp-api.service`, or `openclaw-gateway.service` in the current environment.
- `temporal-dev.service` appears active, but `systemctl` also reported the unit as not-found, suggesting a deleted/transient/stale unit state. Verify before relying on systemd.

Manual restart commands for development:

```bash
cd /root/.openclaw/rmp
nohup /root/.openclaw/rmp/venv/bin/uvicorn app.api.server:app --host 0.0.0.0 --port 8000 > /tmp/rmp-api.log 2>&1 &
nohup /root/.openclaw/rmp/venv/bin/python worker.py > /tmp/rmp-worker.log 2>&1 &
nohup openclaw gateway --force > /tmp/openclaw-gateway.log 2>&1 &
```

After OpenClaw update/reinstall:

```bash
bash /root/.openclaw/rmp/patch_openclaw.sh
pkill -f 'openclaw-gateway' 2>/dev/null
sleep 2
nohup openclaw gateway --force > /tmp/openclaw-gateway.log 2>&1 &
```

Recommended next infrastructure task:

- Create real, persistent systemd unit files for:
  - `rmp-api.service`
  - `rmp-worker.service`
  - `openclaw-gateway.service`
  - `temporal-dev.service` if needed
- Ensure all services restart after reboot.
- Ensure logs go to predictable locations.
- Add health checks.

## 5. Current Database and Workflow State

Database summary from `rmp_db` at handoff time:

- `completed`: 45 tasks
- `failed`: 1 task
- no running/pending tasks in the database

Recent tasks include:

- `3a88e783-5105-46e1-8cb4-bfa713af1cfe`: completed, user asked whether Aura remembers having a Moltbook account.
- `1fbdc304-9dd8-4586-b90b-ef8c511d432f`: completed, user asked which LLM Aura is using.
- `a3255727-138b-4a2c-8bcc-b2a344600301`: completed, heartbeat interval change to every 4 hours.
- `2297f5b7-bb79-43c0-9bdd-624e62a06f21`: completed, heartbeat 30-minute setting question.
- `16e6da00-af0f-42f5-ab2e-f0dc30ed0055`: completed, why HEARTBEAT_OK was being sent.

Temporal query at handoff time returned no `GenericTaskWorkflow` instances in the current list query. This may be because Temporal visibility retention expired, because the server currently has no active RMP workflows, or because of current Temporal state. Re-verify after restarting API/worker/gateway.

## 6. Progress Made So Far

### Research and planning

- Reviewed the original request and three research files.
- Chose a sidecar architecture: OpenClaw as execution surface, RMP as durable controller.
- Chose Temporal + Postgres + FastAPI as the initial stack.
- Identified future memory architecture based on process-scoped memory pools, event logs, and eventual vector/graph memory.

### RMP implementation

- Built FastAPI server and dashboard.
- Built SQLAlchemy schema foundations for tasks, processes, steps, observations, and events.
- Built Temporal workflow and worker.
- Built OpenClaw activities for:
  - sending tasks to OpenClaw
  - polling internal session JSONL
  - parsing task status
  - running quality verification
  - delivering final Slack messages
  - updating database task status
- Added dashboard settings for intermediate updates.
- Added model/provider/API-key configuration from dashboard.
- Added dynamic OpenClaw model catalog loading from `openclaw models list --all --json`.
- Removed misleading `(unavailable)` labels from the dashboard model dropdown because OpenClaw's catalog availability flag did not reliably reflect real usability.

### OpenClaw integration

- Created `/root/.openclaw/plugins/rmp_adapter/index.js`.
- Switched from `api.registerHook()` to `api.on("before_message_write", ...)`, which was required for typed hook interception.
- Added blocking for internal RMP messages and hook auto-delivery messages.
- Added task creation before user messages reach the main agent.
- Added active task signaling for stop/cancel/abort.
- Added active task signaling for follow-up user messages.
- Added `patch_openclaw.sh` for after OpenClaw updates.
- Patched OpenClaw behavior to disable model fallback and improve hook persistence/announcement suppression.

### Model and key work

- Switched from Google Gemini models to OpenAI `openai/gpt-5.4`.
- Removed the user-specified old Google key from active local config/auth profiles/workspace scripts.
- Note: historical logs/backups may still contain old secrets. A full historical secret scrub was not completed.

### Heartbeat and duplicate task work

- Heartbeat was originally every `30m`; changed to `4h` in `/root/.openclaw/openclaw.json`.
- Added workflow behavior to complete plain heartbeat acknowledgements silently.
- Cleaned stale duplicate workflows/tasks that had remained running after earlier routing bugs.

## 7. Logs and Debugging References

Primary logs:

- `/tmp/rmp_plugin_debug.log`
  - Plugin registration, task creation, blocking decisions, signal errors.

- `/tmp/rmp-worker.log`
  - Worker logs and HTTP call traces to OpenClaw/Slack.

- `/tmp/rmp-api.log`
  - RMP API logs when API is started with the manual command above.

- `/tmp/openclaw-gateway.log`
  - OpenClaw gateway startup/runtime logs.

- `/tmp/openclaw/openclaw-YYYY-MM-DD.log`
  - OpenClaw daily logs.

- Temporal UI:
  - `http://localhost:8233/`
  - Usually reached through SSH tunnel from the user's local machine.

- RMP dashboard:
  - `http://localhost:8000/`
  - Currently needs RMP API running.

- Full Cursor transcript:
  - `/root/.cursor/projects/root/agent-transcripts/3ad82511-ec0a-4a67-929b-23f5660a4033/3ad82511-ec0a-4a67-929b-23f5660a4033.jsonl`
  - Use this only when historical context is unclear. Search first; do not read it linearly.

Useful commands:

```bash
pgrep -af 'temporal|worker.py|uvicorn|openclaw-gateway|openclaw gateway'
ss -ltnp
PGPASSWORD=rmp_password psql -U rmp -d rmp_db -h localhost -c "select status, count(*) from tasks group by status;"
openclaw status
```

Avoid exposing secrets when using `openclaw status` or reading config files. It can show token hints or raw values depending on command/output.

## 8. Slack Connection

OpenClaw is connected to Slack in socket mode via `/root/.openclaw/openclaw.json`.

The user interacts with "Aura" in Slack. Aura is the OpenClaw main agent (`agent:main:main`).

RMP uses Slack in two ways:

1. Inbound:
   - Slack DM enters OpenClaw.
   - OpenClaw plugin intercepts before message write.
   - RMP creates and owns the task/workflow.

2. Outbound:
   - RMP posts final cleaned messages directly to the Slack user's DM via Slack `chat.postMessage`.
   - This bypasses OpenClaw's normal delivery path to avoid duplicate or uncontrolled final delivery.

Mapping Slack user:

- `openclaw_activities._get_slack_user_id()` reads `sessions.json`.
- It checks `origin.from` prefixes:
  - `slack:`
  - `user:`
  - `slack:user:`
- It falls back to `origin.id` if it starts with `U`.

Known Slack delivery problem:

- User recently observed final answers containing `[[reply_to_current]]`.
- User also saw two answers for the same question: one normal OpenClaw-style answer and one RMP-delivered answer containing the tag.
- Likely causes:
  - OpenClaw internal session output contains reply-control tags that RMP does not strip.
  - An OpenClaw delivery path still leaks despite `deliver: false` and plugin blocking.
  - Slack streaming/native delivery may send an internal answer before RMP's direct final delivery.
- Immediate fix should include:
  - Strip `[[reply_to_current]]` and similar bracket tags in `notify_slack_user()` and/or `strip_json_eval()`.
  - Audit all OpenClaw outbound delivery paths for internal `rmp_task_` and `rmp_verify_` sessions.
  - Consider disabling native streaming for internal hook sessions if possible.
  - Add plugin blocking for any assistant text containing `[[reply_to_current]]` when it originates from internal RMP sessions or hook delivery.

## 9. Current Model Configuration

Current active model in `/root/.openclaw/openclaw.json`:

- `openai/gpt-5.4`

Heartbeat:

- `4h`

Important model policy:

- Model fallback was intentionally disabled because the user wanted exactly one model used, with no silent fallback.
- `patch_openclaw.sh` re-applies this after OpenClaw updates.

Dashboard behavior:

- Provider/model dropdown is dynamically generated from OpenClaw's model catalog.
- API key can be entered or reused from existing profile.
- Dashboard writes selected provider/model to `openclaw.json`.
- Dashboard writes provider key to `auth-profiles.json`.
- Dashboard restarts OpenClaw gateway after config update.

Known issue:

- `openclaw.json` still contains old Google model entries under `agents.defaults.models`, even though the primary model is OpenAI and the old active Google API key was removed. Consider cleaning these allowed model entries if the single-model invariant should be stricter.

## 10. Task State Model

Implemented statuses:

- `created`
- `running`
- `completed`
- `failed`
- `pending_user_input`
- `stopped_by_user`

Current workflow behavior:

- `POST /tasks` creates DB row with `created`.
- Workflow starts and sets `running`.
- Agent receives an execution prompt requiring final JSON:

```json
{
  "task_status": "completed | pending | failed | stopped_by_user",
  "reason": "Explain why the task is in this state"
}
```

- Workflow parses that JSON.
- `completed` triggers quality verification.
- Material quality fail triggers retry.
- Non-material quality fail is allowed through after the recent softening.
- `pending` triggers retry and optional user update.
- `failed` marks task failed and notifies user.
- `stopped_by_user` marks stopped and notifies user.
- After 10 cycles, workflow asks user whether to continue or stop.

Known weakness:

- The agent self-reports task status, and the verifier is still LLM-based.
- This is better than uncontrolled OpenClaw, but not yet the target architecture where completion is based on objective evidence and task-specific predicates.

## 11. Quality Check Model

Current quality check:

- `verify_response_quality()` asks an internal OpenClaw session to review the answer.
- It is independent from the original execution session but still uses an LLM.
- It returns JSON:

```json
{"quality": "pass", "reason": "..."}
```

or:

```json
{"quality": "fail", "issues": "..."}
```

Recent change:

- The verifier prompt was softened to fail only on material problems.
- Workflow `is_material_quality_failure()` uses keyword markers to decide whether a `fail` should trigger another attempt.

Known problem:

- This can miss subtle wrong answers or over-accept plausible ones.
- Better next version:
  - task-specific validators
  - evidence extraction
  - URL/domain checks
  - web/API verification for external facts
  - structured result schemas
  - verifier requiring citations/artifacts
  - deterministic checks before LLM review

Example issue from earlier testing:

- User asked for most recent Moltbook post and URL.
- Agent answered with a MoltMarket URL.
- User corrected the agent.
- This showed the quality mechanism must catch entity/domain mismatch (`Moltbook` vs `MoltMarket`) before user delivery.

## 12. Known Issues and Technical Debt

### A. Runtime services are not persistently managed

At handoff time, only Temporal/Postgres were running. RMP API, worker, and OpenClaw gateway were not running.

Fix:

- Recreate reliable systemd services.
- Add restart policies.
- Add health checks.
- Add startup ordering: Postgres -> Temporal -> RMP API/worker -> OpenClaw gateway.

### B. Duplicate Slack delivery and raw OpenClaw reply tags

Observed:

- `[[reply_to_current]]` leaked into Slack.
- Two messages were delivered for the same task.

Fix:

- Strip OpenClaw reply-control tags.
- Block all internal-session outbound delivery paths.
- Re-test with Slack.

### C. `ProcessRun`, `Step`, `Observation`, `Event` are underused

Current RMP mostly uses `Task`.

Fix:

- Create `ProcessRun` per Temporal workflow.
- Create `Step` records per execution/verification/notification attempt.
- Create `Observation` records for agent output, tool output, external facts.
- Create `Event` records for all state transitions and signals.

### D. Cron and long-running process integration incomplete

The user explicitly wants cron and background jobs represented as running processes/tasks.

Current:

- Plugin can tag cron messages as `cron`.
- Workspace has Moltbook scanner/process notes.
- No robust reconciler maps OpenClaw cron jobs or external scripts into RMP process rows.

Fix:

- Poll OpenClaw cron list and OS/system timers.
- Upsert corresponding RMP tasks/process runs.
- Track next run, last run, status, output artifacts, and stop controls.
- Make Moltbook/Moltbot scanner tasks first-class RMP processes.

### E. Memory plane not yet fully built

Despite the name, the current implementation is mostly a reliability/task plane.

Fix:

- Add process-scoped memory tables.
- Add memory routing policy.
- Add memory extraction from completed task events.
- Add semantic/vector store or pgvector.
- Add graph/entity memory if useful.
- Keep OpenClaw `MEMORY.md` as a small pinned layer, not the source of truth.

### F. OpenClaw patches are fragile

RMP depends on patches to OpenClaw `dist` JS files.

Fix:

- Upstream or replace with documented extension points if possible.
- At minimum, make `patch_openclaw.sh` idempotent and verified by tests.
- Add post-update verification command.

### G. Shell environment lacks `rg`

The shell currently reported `rg` not found. Cursor's `rg` tool still works, but shell-based scripts that assume `rg` will fail.

Fix:

- Install ripgrep if shell workflows need it:
  - `apt install ripgrep`

## 13. Suggested Immediate Next Steps

1. Restore runtime services:
   - Start RMP API.
   - Start RMP worker.
   - Start OpenClaw gateway.
   - Verify ports `8000`, `18789`, `7233`, `8233`.

2. Recreate systemd units:
   - `rmp-api.service`
   - `rmp-worker.service`
   - `openclaw-gateway.service`
   - Verify `temporal-dev.service`.

3. Fix Slack cleanup/duplicate delivery:
   - Strip `[[reply_to_current]]`.
   - Trace why normal OpenClaw answer and RMP answer both arrive.
   - Ensure internal RMP sessions never deliver directly to Slack.

4. Add real process rows:
   - On task creation, create `ProcessRun`.
   - On every attempt, create `Step`.
   - On every output, create `Observation`.
   - On every state change, create `Event`.

5. Build reconciler:
   - Periodically find stuck tasks.
   - Compare DB state with Temporal workflow state.
   - Repair mismatches.
   - Detect orphaned workflows and orphaned tasks.

6. Add cron/process ingestion:
   - Import OpenClaw cron jobs.
   - Import relevant OS cron/systemd timers/processes.
   - Model Moltbook scanners as controlled RMP processes.

7. Harden quality checks:
   - Add deterministic entity/domain checks.
   - Require evidence for external facts.
   - Use task-type-specific validators.
   - Store verifier decisions in `Observation`/`Event`.

8. Build memory plane:
   - Memory item schema.
   - Scope tags: user, task, process type, process instance, agent.
   - Extraction and compaction policies.
   - Integration with OpenClaw `MEMORY.md` as pinned summary only.

## 14. Testing Checklist for the Next Agent

After restarting services:

1. Open dashboard:
   - `http://localhost:8000/`
   - If remote, use SSH tunnel.

2. Open Temporal:
   - `http://localhost:8233/`

3. Send Slack DM:
   - "What model are you using?"

4. Expected:
   - One RMP task appears.
   - One Temporal workflow appears.
   - Original main-agent response is blocked.
   - Final Slack answer has no `[[reply_to_current]]`.
   - Final Slack answer has no trailing `{"task_status": ...}` block.
   - Task status becomes `completed`.
   - Workflow status becomes `completed`.

5. Test wrong-answer quality:
   - Ask for a Moltbook URL and verify not MoltMarket.
   - Expected: if answer is wrong domain/entity, verifier should retry before delivery.

6. Test stop:
   - Ask for a slow task.
   - Send "stop".
   - Expected: active workflow receives signal, task becomes `stopped_by_user`.

7. Test follow-up:
   - Ask a task.
   - While it is running, send a clarification.
   - Expected: clarification is signaled into existing task, not a new duplicate task.

8. Test heartbeat:
   - Confirm `openclaw status` shows heartbeat `4h`.
   - Plain `HEARTBEAT_OK` should not appear in Slack.

## 15. Short Glossary

- Aura: the Slack-facing OpenClaw main agent/bot.
- OpenClaw gateway: the Node.js runtime/gateway, usually port `18789`.
- RMP: Reliability and Memory Plane, the sidecar control plane.
- Task: user-visible durable record in Postgres.
- Workflow: Temporal `GenericTaskWorkflow`, one per task.
- ProcessRun: intended durable process row; schema exists but not yet fully used.
- Step: intended row for each workflow/activity step; schema exists but not yet fully used.
- Observation: intended row for facts/artifacts/output; schema exists but not yet fully used.
- Event: intended audit log/state transition row; schema exists but not yet fully used.
- `agent:main:main`: main Slack/user session.
- `agent:main:rmp_task_<task_id>`: internal execution session.
- `agent:main:rmp_verify_<task_id>`: internal quality review session.
- Moltbook/Moltbot: workspace-related account/API/scanner context; scanner scripts and logs live under `/root/.openclaw/workspace`.

## 16. Most Important Guidance for KIMI

Do not treat the current implementation as finished. It is a functional prototype of interception, task creation, Temporal execution, quality review, and dashboarding. The user wants a production-ready system where:

- every user message is intercepted,
- every task has durable state before execution,
- every long-running process is monitored,
- task completion is based on evidence and deterministic predicates,
- OpenClaw/LLMs are workers, not the state authority,
- Slack receives one clean answer,
- users can stop/continue work,
- memory is scoped by task/process,
- updates to OpenClaw do not break the RMP.

The next development phase should focus on robustness: persistent services, duplicate-delivery cleanup, first-class process/step/event logging, reconciler, cron/process ingestion, and evidence-based quality checks.
