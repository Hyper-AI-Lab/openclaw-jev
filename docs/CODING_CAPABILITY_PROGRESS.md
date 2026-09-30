# Aura codes with Claude Code, on OpenClaw 2026.9.7: progress log

Append-only. One entry per plan step: what changed, files, verification, deviations and why.

- **Plan:** `/root/.cursor/plans/aura_codes_with_claude_code_b71786a2.plan.md`.
- **Where the work happens:** the worktree `/root/.openclaw/rmp-coding` (branch `coding-capability`). `main` is fast-forwarded after each step.

---

## Step 1 — Baseline, progress log, research brief

**Date:** 2026-09-30.

**Baseline (host):**
- Ubuntu 24.04.3, kernel 6.8.0-138, systemd 255.
- 4 vCPU, 7 GB RAM (about 4 GB available), 115 GB of free disk.
- Node v22.23.2 from NodeSource `node_22.x`, OpenClaw 2026.9.1 (`ad6fe23`), Python 3.12.3.
- `nft`, `systemd-run`, `git` and `gh` are present; `bwrap` and `socat` are not. There is no `claude` binary and no `aura-coder` user.
- Readiness: 38 pass, 1 warn (telemetry, by design), 0 fail.
- `main` is at `e479f72`, CI green (run 36713458996).

**Loopback listeners** (they set `aura-coder`'s firewall):

| Service | Ports |
|---|---|
| sshd | 22 |
| OTLP collector (docker) | 4317, 4318 |
| Phoenix (docker) | 6006 |
| Postgres | 5432 |
| Qdrant | 6333, 6334 |
| Temporal | 6933-6935, 6939, 7233-7235, 7243 |
| RMP API | 8000 |
| Web stack | 8791 |
| Obscura CDP | 9222 |
| OpenClaw gateway | 18789 (IPv4 and IPv6) |
| systemd-resolved (DNS) | 53 (stays reachable) |

Listeners in the ephemeral range are test servers and containerd's streaming endpoint.

**Research brief, Claude Code** (current docs and issue tracker):
- **Releases:** `stable` 2.1.280, `latest` 2.1.285 (npm dist-tags, 2026-09-30).
  - The native installer takes an exact version (`curl -fsSL https://claude.ai/install.sh | bash -s <version>`). It installs `~/.local/bin/claude` as a launcher into `~/.local/share/claude/versions/`, and auto-updates by default.
  - `DISABLE_UPDATES=1` in the settings `env` block blocks every update path. `DISABLE_AUTOUPDATER` only stops the background check, and native installs have kept updating despite `autoUpdates: false` (claude-code#56723). [code.claude.com/docs/en/setup]
- **Headless mode:**
  - `claude -p --output-format stream-json --verbose` emits one JSON event per line: `system/init`, `assistant`, `user`, `result`, `rate_limit_event` and `system/api_retry`.
  - `--json-schema` returns a validated `structured_output` in the result, `--resume <session_id>` continues a session, and `--max-turns` bounds a run. The result reports `total_cost_usd`, a client-side estimate.
  - `--bare` skips hooks, plugins, MCP and CLAUDE.md, but never reads OAuth credentials, so it can't use a subscription token.
  - Without `--bare`, `-p` runs a repository's `.claude/settings.json` hooks and connects its `.mcp.json` servers with no trust prompt. [code.claude.com/docs/en/headless]
- **Auth:**
  - `claude setup-token` runs the browser OAuth flow and prints a one-year token for a Pro, Max, Team or Enterprise plan, used as `CLAUDE_CODE_OAUTH_TOKEN`. The token can only make model requests.
  - The printed token wraps across two terminal lines, and copying only one line gives a 401 (claude-code#54738, #65320).
  - `setup-token` also writes a stored session into `~/.claude`, which some modes prefer over the env token (#80091). [code.claude.com/docs/en/authentication]
- **Policy:**
  - OAuth is meant for subscribers' ordinary use of Claude Code and Anthropic's native apps; developers offering products must use API keys. Advertised limits assume ordinary individual use.
  - `claude -p` draws from the subscription's limits; the June 2026 plan for a separate Agent SDK credit is paused.
  - Kirill's own assistant calling the official CLI on his own server fits this. [code.claude.com/docs/en/legal-and-compliance; support.claude.com/en/articles/15036540]
- **Permissions:**
  - Modes are `default`, `acceptEdits`, `dontAsk` and `bypassPermissions`; the last is refused as root.
  - In `-p` mode `PreToolUse` hooks and `--allowedTools` are not enforced (claude-code#33343).
  - Managed `permissions.deny` rules hold inside a bypass session (#86253). But the docs say a Bash rule "isn't a security boundary around the program": it matches the command text, so it blocks `git push origin main` and not `git -C . push origin main`.
  - `disableBypassPermissionsMode` only downgrades the mode (#44642). [code.claude.com/docs/en/permissions]
- **Managed settings on Linux:** `/etc/claude-code/managed-settings.json` plus `managed-settings.d/*.json`. Users can't write them and no other scope overrides them.
  - Lockdown keys: `allowManagedHooksOnly`, `allowedMcpServers: []` with `allowManagedMcpServersOnly`, `disableClaudeAiConnectors`, `disableSideloadFlags`, `disableSkillShellExecution`, `strictKnownMarketplaces: []`, `allowManagedPermissionRulesOnly`, `cleanupPeriodDays`. [code.claude.com/docs/en/settings]
- **Sandbox:** bubblewrap plus socat on Linux, for Bash commands only; file tools follow the permission rules. Neither is installed here. [code.claude.com/docs/en/sandboxing]
- **Rate limits:**
  - `rate_limit_event` carries `rate_limit_info`:
    - `status`: `allowed`, `allowed_warning` or `rejected`;
    - `resetsAt`, in seconds or milliseconds;
    - `rateLimitType`: `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet` or `overage`;
    - an optional `utilization`.
  - `system/api_retry` names the error: `authentication_failed`, `rate_limit`, `overloaded`, and so on. [claude-code#78476, #50518]
- **Models:** the docs' examples name `claude-opus-5-5` and `claude-sonnet-5`. The runner uses the `opus` and `sonnet` aliases rather than dated IDs.

**Research brief, OpenClaw:**
- **Breaking changes:** 2026.9.3 is the only release since 2026.9.1 with any.
  - Node 24.16+ on 24.x, or 26.1+ (Node 26 recommended), to prevent SQLite text truncation.
  - Plugin SDK moves: execution-policy helpers, approval SDK, SDK aliases, search and directory result callbacks, Workshop skills. RMP's plugins use none of these.

  2026.9.4 to 2026.9.7 list no breaking changes. [docs.openclaw.ai/releases/2026.9.3; unpkg CHANGELOG 2026.9.3–2026.9.7]
- **2026.9.7 compresses transcripts.** Verified in its dist; the changelog doesn't mention it.
  - Which events: 1024 UTF-8 bytes or more, whenever compression saves at least max(64 B, 10%).
  - How they're stored: zstd level 1 with checksum, `event_json` NULL, plus `event_zstd`, `event_utf8_bytes` and a `navigation_json` projection.
  - Decoding goes through a SQLite function registered only inside OpenClaw. Existing history is compressed during the migration.
- **2026.9.7 ships its dist mostly as `.mjs`,** while RMP's patcher and verifier read only `*.js`. With `.mjs` included:
  - patches 6c and 6d (OpenAI first byte, max effort), 7 (session placeholder skip) and 9 (local placement cleanup) no longer match;
  - 6e is satisfied upstream.
- **Multiple gateways:** each instance needs its own `OPENCLAW_CONFIG_PATH`, `OPENCLAW_STATE_DIR`, workspace and ports, and OpenClaw enforces ownership of the state directory. [docs/gateway/multiple-gateways.md]
- **ACP route:**
  - The `@openclaw/acpx` backend is excluded from the 2026.9.1 package.
  - Headless ACP writes fail by default (`nonInteractivePermissions=fail`).
  - Turns use OpenClaw's run timeouts. [docs.openclaw.ai/tools/acp-agents, acp-agents-setup]

**RMP facts that shape the design** (read-only exploration):
- **The approval gate reads the wrong text.** The catalog gate matches its approve and stop patterns against the whole catch-up brief, not just Kirill's words, and waits with no timeout. `user_words()` exists but isn't called.
- **The evaluator can't see outside work.** It checks claims only against OpenClaw transcripts, and `task_action_trace` overwrites any trace the caller passes, so work done outside OpenClaw reads as "no tool calls".
- **Long activities get killed.** A 90-minute activity dies to any of:
  - the reconciler: it flags a task after 20 minutes without a `tasks.updated_at` change and terminates it after 45;
  - the janitor: after 2 hours, for workflow IDs it doesn't recognize;
  - any worker restart, which kills the cgroup.
- **Localhost is trusted.** Temporal on 127.0.0.1:7233 accepts unauthenticated clients, and several RMP endpoints skip the API key for localhost.
- **Writing `*.py` under `rmp/app` is a deploy.** `rmp-code-watch` reloads once idle, approved or not, and the current `tool_self_upgrade` draft step may write there.
- **The agent database isn't backed up.** Neither `upgrade_openclaw.sh` nor the nightly backup copies `openclaw-agent.sqlite` (96 MB).
- **`zstandard` is installed but unpinned:** 0.25.0, pulled in by `langsmith`.

**Decisions this confirms** (as in the plan):
- An RMP-owned runner calling the official CLI in `-p` mode with a `setup-token` token.
- A dedicated `aura-coder` user as the security boundary, running Claude Code in `bypassPermissions` mode inside it, under managed lockdown settings. Deny rules are a second line only.
- Claude Code pinned to the stable channel (2.1.280), with updates disabled.
- One job at a time.

**Verification:**
- The worktree `/root/.openclaw/rmp-coding` was created on branch `coding-capability` from `main` (`e479f72`). Its `venv` is a link to the main checkout's venv, as in `rmp-deep`.
- Full suite: 795 passed, 4 skipped, in 814 s. That is slower than the 463 s this morning, because of host load (load average 3.5 on 4 cores, I/O pressure about 15%).
