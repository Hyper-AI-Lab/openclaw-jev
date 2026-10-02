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

---

## Step 2 — Claude Code on the host

**Date:** 2026-09-30.

**What changed:**
- `app/coding/` (new package):
  - `units.py`: the hardening every coding unit shares.
    - Sandboxing: `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp`, `PrivateDevices`, `NoNewPrivileges`, an empty capability set, the kernel and cgroup protections, `UMask=0077`.
    - Hidden (`InaccessiblePaths`): `/root`, `/etc/aura-coder`, `/etc/rmp`, `/etc/openclaw`, Postgres' data directory and socket, the D-Bus system bus, the Docker, containerd, snapd and lxd sockets, and `/run/user`.
    - Writable (`ReadWritePaths`): only what the caller names.
    - Limits: memory, no swap, tasks, CPU, `RuntimeMaxSec`, and lower CPU and I/O weight than the production services.
    - The token arrives through `EnvironmentFile`, which systemd reads as root.
    - `systemd_run_argv` builds a collected transient service that runs as `aura-coder`.
  - `firewall.py`: the nftables table `inet aura_coder`, replaced atomically on each apply. `active()` requires all four user rules, and `uncovered_listeners()` lists loopback listeners below the ephemeral range that the rules leave open (DNS is open by design). For `aura-coder` it rejects:
    - loopback TCP and UDP to `coding.blocked_tcp_ports`;
    - all traffic to 10/8, 100.64/10, 169.254/16, 172.16/12, 192.168/16, fc00::/7 and fe80::/10.
  - `credentials.py`: finds the `setup-token` token in a terminal transcript, including a wrapped one, and validates it. It writes `/etc/aura-coder/claude.env` (0600) and a metadata file (issue date, expiry, length, fingerprint) atomically. `python -m app.coding.credentials extract|store` gives the login script both steps.
- `app/config.py`: a `coding` settings section with the pinned version, models, limits and the blocked ports.
- Setup and login:
  - `ops/setup_aura_coder.sh` (idempotent): the user, `/srv/aura-code/{jobs,venvs,cache}`, `/etc/aura-coder`, Claude Code 2.1.280 installed with the native installer, `/etc/claude-code/managed-settings.json` from `ops/aura_coder/managed-settings.json`, git identity "Aura (Claude Code)", and the firewall unit.
  - `ops/claude_code_login.sh`: runs `setup-token` as `aura-coder` in a throwaway home inside a 400-column terminal, then extracts, stores and smoke-tests the token. The `--paste` mode also accepts a token pasted across two lines.
- Firewall wiring: `ops/aura_coder_firewall.sh`, and `systemd/aura-coder-firewall.service` (oneshot, enabled, ordered before the RMP services).
- `ops/claude_code_smoke.py`:
  - preconditions: user, pinned version, managed settings identical to the repo copy, firewall active;
  - 32 isolation probes in a hardened unit;
  - a real `claude -p` call, trying the configured model and then the fallback;
  - the result recorded at `data/coding/claude_smoke.json`.
- The managed settings are:
  - `DISABLE_UPDATES` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`;
  - deny rules for `git push`, `gh`, `sudo`, `su`, `systemctl`, `ssh`, `scp`, `nft` and `iptables`;
  - `allowManagedPermissionRulesOnly`, `allowManagedHooksOnly`, and `allowManagedMcpServersOnly` with an empty `allowedMcpServers`;
  - `disableClaudeAiConnectors`, `disableSideloadFlags`, `disableSkillShellExecution`, an empty `strictKnownMarketplaces`, and `cleanupPeriodDays` 30.
- Tests: `tests/test_coding_host.py` (17): unit hardening and the `systemd-run` argv, port validation, the ruleset, `active()`, uncovered listeners, token extraction (plain, with escapes, wrapped, absent), token storage modes and metadata, the login commands as the script calls them, settings defaults, script syntax.

**Host changes:**
- System user `aura-coder` (uid 997, password locked, no sudo).
- `/srv/aura-code` and `/etc/aura-coder`.
- Claude Code 2.1.280 at `/home/aura-coder/.local/bin/claude`.
- `/etc/claude-code/managed-settings.json`.
- `aura-coder-firewall.service`, enabled and active.

**Deviations and why:**
- **Install before managed settings.** The installer refuses to run while the managed settings set `DISABLE_UPDATES` ("Updates are disabled by your administrator"). The setup therefore installs the pinned version first, and removes the managed file for the duration of any later version change.
- **nft lists the user by uid** once it resolves the name (`meta skuid 997`), so `active()` accepts either form.
- **The probes found Postgres' Unix socket connectable.** Authentication would still refuse `aura-coder`, which has no role, but the units now hide every sensitive socket by its `/run` path. `/var/run` is a symlink, and the probe covers both paths.
- **Beyond the plan's loopback ports:** the firewall also rejects private, link-local and carrier-grade ranges. These cover the Docker bridge, since containers are reachable there, and the cloud metadata address.
- **DNS is open by design** (systemd-resolved on 127.0.0.53), so the uncovered-listener report excludes port 53.
- **Login bug.** The first login captured the token, but its inline Python quoting broke storing it. `store` and `extract` became module commands with tests, and the second run stored the token. The first token Kirill created stays valid and unused.
- **Findings for the runner (step 7):**
  - `--max-turns` is accepted although `--help` no longer lists it.
  - `--max-budget-usd` exists.
  - `claude -p` exits 0 even on an API error (`terminal_reason: api_error`), so success must come from the result fields.

**Verification:**
- Tests: 17 passed.
- The setup ran twice. The second run changed nothing and finished with the firewall active and no uncovered listeners.
- Isolation, as `aura-coder` in a hardened unit, 32 probes as intended:
  - Refused or unreadable:
    - reading `/etc/rmp/rmp.env`, `/etc/openclaw/openclaw.env`, `/etc/aura-coder/claude.env`, `/root/.config/github_pat`, `openclaw.json` and `settings.json`;
    - listing `/root`;
    - connecting to 22, 5432, 6333, 7233, 8000, 8791, 9222 and 18789 (IPv4 and IPv6);
    - connecting to 172.17.0.1:6333 and 169.254.169.254;
    - the Docker, Postgres (both paths), D-Bus, snapd and containerd sockets;
    - writing to `/srv/aura-code/venvs`, `/etc`, `/usr/local/bin` and `/root/.openclaw`.
  - Allowed: the job directory, its home, its own ephemeral test server, and api.anthropic.com:443.
- Claude Code: `opus` answered as `claude-opus-5-5` in 7.1 s, over 2 turns, using the Read tool, in `bypassPermissions` mode with no MCP servers. So Kirill's plan allows Opus, and `opus` stays the default model.
- The token is 108 characters, root-only, and expires 2027-09-30.
- The version is still 2.1.280, with one version installed.

---

## Step 3 — Backups and compressed transcripts (2026.9.1 and 2026.9.7)

**Date:** 2026-09-30.

**Research:**
- **What 2026.9.7 compresses.** Read in the 2026.9.7 dist (`transcript-payload-*.mjs`, `prepareTranscriptPayload`): events of at least 1024 UTF-8 bytes are compressed at zstd level 1 with a checksum, whenever that saves at least max(64 B, 10%).
  - Skipped: events containing `\u` escapes or NUL, and session headers.
  - Stored: `event_json` NULL, `event_zstd`, `event_utf8_bytes` and a `navigation_json` projection.
  - The limit is `MAX_COMPRESSED_EVENT_BYTES` = 4 MiB.
- **Decoding in Python.** `zstandard` decodes frames written by Node's `zlib.zstdCompressSync` (test below). `max_output_size` is only an upper bound, because the frames carry their content size.

**What changed:**
- `app/openclaw_transcripts.py` (new) is the one decode point:
  - `event_columns(con, alias)` selects `(event_json, event_zstd, event_utf8_bytes)` when the column exists, else `(event_json, NULL, NULL)`;
  - `decode_event` returns the JSON text, or None for a frame it cannot decode (logged). It uses a decompressor per call, because zstandard contexts are not thread-safe.
- The readers use it: `app/openclaw_sessions.py` `read_transcript_lines` (reply polling, session recovery, the evaluator's action trace, deep-memory ingestion, the canary check), and `app/llm/usage_monitor.py` `scrape_openclaw_sessions` and `transcript_usage`.
- `requirements.txt`: `zstandard==0.25.0`, the version already installed through `langsmith`.
- `ops/backup_openclaw_state.py` (new):
  - `backup` uses SQLite's backup API for `openclaw-agent.sqlite` and `state/openclaw.sqlite`: a consistent snapshot while the gateway writes.
  - Each copy becomes a self-contained rollback-journal file, is checked with `PRAGMA quick_check`, and is described in `manifest.json`: bytes, sha256, the source's mode and owner, and the OpenClaw version.
  - `verify` checks the checksums and integrity.
  - `restore --yes` refuses while the gateway runs, verifies first, restores mode and owner, and removes a stale `-wal`/`-shm`, which SQLite would otherwise replay onto the restored file.
- The two callers:
  - `ops/upgrade_openclaw.sh` takes this backup into `openclaw-update-*/openclaw-state` before anything stops, and dies if it fails. It replaces the old hot `cp` of the state database (without its WAL), and the rollback hint names the restore command.
  - `ops/backup.sh` (nightly): adds `openclaw-state` to each nightly backup.
- `ops/openclaw_preflight.py`: `transcript_layouts` classifies each transcript table definition as `plain`, `zstd` (what the reader decodes) or `unknown`. It refuses unknown layouts, or no definition at all.
- Tests:
  - `tests/openclaw97.py` (new helper): the 2026.9.7 schema, with events stored the way 2026.9.7 stores them.
  - `tests/test_openclaw_transcripts.py` (6):
    - decode, including a frame compressed by Node's zlib;
    - the 2026.9.1 schema reads unchanged;
    - reply polling finds its marker in a compressed user turn;
    - session recovery, deep-memory tool results and the memory canary read compressed history;
    - usage accounting counts compressed turns.
  - `tests/test_backup_openclaw_state.py` (4): a consistent snapshot while a writer holds an open transaction; a tampered copy fails verify; restore replaces the store, drops a stale WAL and keeps modes; restore refuses while the gateway runs or without `--yes`.
  - `tests/test_openclaw_preflight.py`: layouts, with 2026.9.7's real definition accepted and an unknown one refused.

**Deviation:**
- The first live backup left `-wal`/`-shm` sidecars: the copies kept WAL mode, and reading them created the files. The copies now switch to `journal_mode=DELETE` before their check, and a test asserts the directory holds exactly the manifest and the two databases.

**Verification:**
- Full suite: 823 passed, 4 skipped (317 s).
- **Live reads unchanged on 2026.9.1:** the old reader (`main`) and the new one (worktree) returned identical results on the live agent database:
  - the 25 most recent sessions: 673 lines, content hash `ea81d60f41290267`;
  - `transcript_usage(24 h)`: 251 attempts, 802,247 input, 4,778,951 cache-read and 295,364 output tokens.
- **Live backup:** agent 92 MiB and state 18 MiB in 5.0 s, with the gateway running; `quick_check` ok and `verify` ok. Kept at `data/backups/openclaw-state/20260930T143147Z`.

---

## Step 4 — Patcher and verifier for 2026.9.7 (still correct on 2026.9.1)

**Date:** 2026-09-30.

**Research:** read in the pristine `npm pack` packages of both versions.
- **Where the targets are:** 2026.9.7 ships most of its dist as `.mjs`, and repeats most patch targets in `package-update-activation-recovery.mjs`, a 66 MB recovery bundle.
- **How that bundle declares constants:** esbuild-style, as a hoisted `var DEFAULT_LLM_IDLE_TIMEOUT_MS, …` assigned inside an init function (`\tDEFAULT_LLM_IDLE_TIMEOUT_MS = 12e4;`).
- **Minified copies:** worker bundles (`worker/worker.mjs`, `sqlite-store.worker.mjs`) hold some of the same functions minified. 2026.9.1 has always run with those copies unpatched.
- **Patches 6c and 6d:** of 6c's five edits, only one anchor moved (2026.9.7 opens the closure with a `trackCleanup` line). `timeoutOptions.model.provider` and the stream `options.reasoning` are unchanged, so 6d applies once 6c does.
- **Patch 7:** `validateCanonicalSessionRow`/`…RowEntry` still refuse a row whose `entry_valid` is 0 when admitting it. Only `{}` placeholders with -1 are skipped. The problem is not fixed upstream, so the patch is **rewritten, not dropped**.
- **Patch 9:** the per-placement `resolveWorkspacePath` loop is gone. Cleanup was reworked (`cleanupPendingWorkspaceResultOrphans` over a change snapshot), and the changelog lists "reduce startup placement work" and "placement claims now run off the Gateway main thread". So patch 9 is **required only where its target exists** (2026.9.1). Step 5's staging boot on real data confirms it.

**What changed:**
- `ops/openclaw_patch_audit.py` (new) holds the single list of the 15 patches. For each: its marker, the exact code it rewrites, and a condition when a release may not need it.
  - A dist passes when every required marker is present and none of the rewritten code remains in any `.js` or `.mjs` file. That catches a patch landing in one copy but not the other.
  - Minified copies never match the rewritten code, so they don't cause false failures.
  - It is used by the patcher and the verifier alike.
- `patch_openclaw.sh`:
  - scans `.js` and `.mjs` (candidates and the full-scan fallback);
  - finishes with the audit instead of per-marker checks, so it fails on any unpatched copy;
  - patch 6 handles the `const` and the hoisted-`var` forms;
  - 6c anchors its closure edit on the `createIdleTimeoutError` line;
  - 6c and 6d write 20 s, 120 s and 30 s inline, because a new `const` inside the recovery bundle's init function would be out of scope for `streamWithIdleTimeout`;
  - patch 7 covers 2026.9.7's row validation and runs on any file with any of its targets. It also reports only when it changes a file, so a second run is clean.
- `ops/verify_openclaw_patch.sh`: `OPENCLAW_DIST_DIR` override, the audit instead of its own lists, and the skill path taken from the dist.
- Tests:
  - `tests/fixtures/openclaw_dist/{2026.9.1,2026.9.7}`: 192 KB, 36 files, cut from the real packages by `build.py`. The script keeps only the regions around the patch targets and can rebuild the fixtures for a future release.
  - `tests/test_patch_openclaw.py`, per version: every patch lands and the audit is clean; a second run leaves the files byte-identical; putting one pristine copy back fails, naming exactly that file, and patching again heals it. Also: patches a release no longer needs aren't required; the pre-flight rehearses with the same patcher.
  - `tests/test_openclaw_preflight.py`: the empty-dist message check is loosened, because the audit now reports every missing patch.

**Deviations and why:**
- **Beyond the plan:**
  - Patch 6 needed the hoisted-`var` form. The audit found the recovery bundle kept the 120 s idle limit.
  - 6c and 6d no longer declare `RMP_OPENAI_*` constants.
- **The live 2026.9.1 dist** keeps the named-constant form that the old patcher wrote. It behaves the same, and it passes the audit: its markers are there and none of the rewritten code remains.
- **I built the fixtures with a deterministic script** rather than through a subagent, because the anchor list lives in this step's own research.

**Verification:**
- Tests: 20 patcher and pre-flight tests passed; full suite 827 passed, 4 skipped (448 s).
- Patching full copies of the pristine packages:
  - 2026.9.1: 14 files patched, all 15 patches in place, and a second run changed nothing.
  - 2026.9.7: 15 files patched, including 12 patches in the recovery bundle. All 15 patches are in place, and a second run changed nothing.
- `node --check` passes on every patched file of both versions, as on their pristine originals.
- The pre-flight on `openclaw@2026.9.7` reports only "needs Node >=24.16.0 <25 || >=26.1.0; this host runs v22.23.2". `openclaw@2026.9.1` passes.
- The live verifier passes: the audit is clean on the installed dist, the MoltMarket skill is present, and the model policy holds.

---

## Step 5 — Node 24 and a staging 2026.9.7 on real data

**Date:** 2026-09-30.

**Research:** read in the 2026.9.7 package's docs.
- `gateway/multiple-gateways.md`:
  - each instance needs its own config path, state directory, workspace and port;
  - base ports must be at least 120 apart, because derived ports reach base + 110;
  - `OPENCLAW_STATE_DIR` alone does not isolate a managed service.
- `help/environment.md`: `OPENCLAW_HOME` relocates every OpenClaw path default, and explicit path variables take precedence over it.
- `gateway/health.md`:
  - `/healthz`: the HTTP server is live;
  - `/startupz`: startup has settled;
  - `/readyz`: agent databases are admitted and channels pass. 2026.9.1 serves it too.
- Cron: `cron.enabled: false` or `OPENCLAW_SKIP_CRON=1` disables it.
- 2026.9.7 still exports `./plugin-sdk/gateway-runtime` with `callGatewayFromCli`, which RMP's abort helper uses.
- Node: v24.21.0, the current 24.x LTS ("Krypton", 2026-09-07), satisfies `>=24.16.0 <25`.

**What changed:**
- `ops/openclaw_staging.py` (new) builds a staging gateway on a copy of production's data. Subcommands: `node`, `build`, `start`, `stop`, `status`, `cli`, `install`, `restore-pre`, `destroy`.
  - **Separate tree and port:** its own `OPENCLAW_HOME`, `HOME`, state directory, config and workspace under `/srv/openclaw-staging`, on port 19789.
  - **Cut off from production's outside connections:** Slack off with its tokens removed, cron off in the config and through `OPENCLAW_SKIP_CRON`, fresh gateway and hook tokens. The RMP plugin's copy calls a closed port.
  - **Copied data:** the stores through the backup API, then 2,059 stored production paths rewritten in the operational tables (agent database lease, workspace, cron store, exec-approvals socket, plugin installs). The copied lease is dropped. History tables with integrity chains are left as they were.
  - **The production upgrade's order:** plugins update, then `doctor --fix` with `OPENCLAW_SERVICE_REPAIR_POLICY=external`, then TOOLS.md restore and skill links, settle, patch and audit.
  - **Hardened units:** every staging process runs in a transient unit with `ProtectSystem=strict`, `ProtectHome=read-only` and only `/srv/openclaw-staging` writable. The systemd and D-Bus sockets and `/etc/rmp` are hidden. Commands are resolved to the staging binaries.
- `ops/openclaw_probe.py` (new, reusable for production). Nine probes, each through RMP's own code paths:
  - `health`: `/healthz`, `/startupz` and `/readyz`;
  - `plugins`: every configured plugin loads, RMP's own included;
  - `hook_run`: a `/hooks/agent` run with RMP's payload, read back through RMP's reader and reply poller, including whether the marker turn was compressed;
  - `bootstrap`: an RMP session gets no persona files;
  - `session_entry`: `patch_session_entry` leaves the row pending (`entry_valid` 0), and runs on that session and a new one still work;
  - `settle`: the settle script on the migrated schema;
  - `abort`: `sessions.abort` through RMP's SDK helper, retried while the session still shows running;
  - `history`: pre-migration sessions read back unchanged, as a prefix;
  - `patched`: the running dist passes the patch audit.
- `ops/rollback_openclaw.sh` (new; the plan puts it in step 6, but it is written here so that the rehearsal ran the real script).
  - Steps: refuse while user tasks run, stop, reinstall the version from before, restore the stores, restore config, local plugins and workspace files, reinstall each npm plugin at its old version, settle, audit, start, wait for `/readyz`.
  - `ROLLBACK_TARGET=staging` swaps only the primitives.
- `app/coding/units.py`: `ProtectProc=invisible` (see the findings). `ops/claude_code_smoke.py` gains two probes: another user's processes, and `/proc/1/cmdline`.
- Tests: `tests/test_openclaw_staging.py` (4) covers the staging config, the stored-path rewrite and lease drop, the plugin isolation, and staged commands in hardened units with the staging binaries. `tests/test_coding_host.py` now requires `ProtectProc=invisible`.

**Host changes:**
- Node v24.21.0 at `/opt/node-v24.21.0-linux-x64` (sha256 verified), linked from `/opt/node24`, not on `PATH`. `node` on `PATH` is still v22.23.2.
- `/srv/openclaw-staging`: the staging tree, now stopped. It is removed after the cutover.

**Findings for the cutover (step 6):**
1. `doctor --fix` refuses while an agent database lease is held by a live gateway. The upgrade stops the gateway first.
2. 2026.9.7's doctor archives the workspace `TOOLS.md`, whose content `AGENTS.md` already carries. The upgrade's `restore_tools_md` puts it back.
3. Doctor disables the MoltMarket skill as unusable: it needs `MOLTMARKET_API_KEY`, which production doesn't have. Nothing is lost.
4. 2026.9.7's `plugins update` creates new plugin generations and deletes the old ones. So a rollback must reinstall each plugin at its old version, which the script does.
5. `openclaw.json` must be backed up before any 2026.9.7 command touches it: `plugins update` writes metadata (`meta.migrations.utilityModelSeparation`) that 2026.9.1 rejects. The upgrade backs up first.
6. Changes that don't affect RMP:
   - on start, 2026.9.7 posts an `[OpenClaw session event]` into `agent:main:main`, with no reply;
   - it keeps `systemPromptReport` and other large entry fields in `session_entry_snapshots`, which RMP doesn't read.
7. `session_transcript_cold_archives` is empty after the migration. If 2026.9.7 later moves old sessions there, RMP's reader won't see them, which matters little because RMP reads recent sessions.
8. The staging gateway's memory reached 1.5 GiB, OpenClaw's warning threshold.
9. `sessions.abort` can answer `no-active-run` just after a session turns running. RMP's stop aborts once and doesn't retry. The fix belongs with step 10's stop handling.
10. **Security:** the Temporal container gets its Postgres password as a command-line argument, visible in any process list. So coding units now hide other users' processes.
11. **Inline secrets:** the live `openclaw.json` holds secrets inline (Slack tokens, the hooks and gateway tokens, and skill API keys), against the secrets rule. That predates this work and is not changed here.

**Deviations and why:**
- **Rollback script moved earlier:** `ops/rollback_openclaw.sh` is written in step 5, not step 6, so the rehearsal exercised the real script.
- **Bugs caught in the staging tool before they did any harm:**
  - systemd looks commands up on its own `PATH`, so staged `openclaw` commands would have run production's `/usr/bin/openclaw`. Executables are now resolved to absolute paths.
  - The copied lease blocked doctor.
  - The first rehearsal backup was taken after a 2026.9.7 command had already changed the config. `save_upgrade_backup` now runs before any 2026.9.7 command.
- **Probe fixes:** two probe bugs were fixed. The `/healthz` body's `status` shadowed the HTTP code, and the prompt report now lives in snapshots.
- **ProtectProc:** the Step 2 area, fixed here where it was found.
- **No subagent for the probe suite:** I wrote it myself, because it runs next to production.

**Verification:**
- **Staging build on real data:** 551 s. 2026.9.7 installed and patched (all 15), plugins updated to 2026.9.7, doctor complete after the lease drop, settle ok.
- **Boot:** ready 54.0 s after the unit started, without patch 9. Production 2026.9.1, with patch 9, took 73 s at its last start. So patch 9 stays required only where its target exists.
- **Probes on staging 2026.9.7: 9/9 passed.**
  - `hook_run`: `PROBE_OK` in 6.6 s, with the marker turn stored compressed.
  - `bootstrap`: no persona files injected.
  - `session_entry`: the row stayed pending after the patch, and later turns on it and on a new session worked.
  - `settle`: ok on 2026.9.7's triggers, which add `canonical_pending_*`.
  - `abort`: `aborted` on the first try at 3.6 s; the session ended `killed`.
  - `history`: 40 sessions and 779 lines unchanged; 566 of 15,694 events compressed.
  - `plugins`: 8 loaded. `patched`: the audit is clean.
- **Rollback rehearsal with the real script** (`ROLLBACK_TARGET=staging`): back to 2026.9.1 in 237 s, ready after 19.5 s, and the probes passed 9/9 on the rolled-back gateway. History is identical, with no appends and 0 compressed events.
- **Coding sandbox:** smoke with `ProtectProc`: 34 isolation probes as intended, and `claude opus` ok in 6.7 s.
- **Production untouched:** readiness 38 pass, 1 warn (telemetry), 0 fail, the same as the baseline. The gateway's `/readyz` returns 200.
- **Tests:** full suite 831 passed, 4 skipped (296 s).

---

## Step 6 — Production cutover

**Date:** 2026-10-01, between 22:21Z and 23:15Z on 2026-09-30. Kirill approved both windows back to back, and no user task was active.

**Window 1, Node 24:**
- Online backup of both stores, as a rollback point.
- NodeSource `node_22.x` switched to `node_24.x`, then `apt-get install nodejs`: 22.23.2-1nodesource1 became 24.21.0-1nodesource1, with npm 11.19.0.
- `ops/restart_rmp.sh`, then the kairos daemons restarted on Node 24 through their cron script.
- Timing: apt 36 s, restart to `/readyz` 33 s, 69 s in total. Aura was offline for about 33 s.

**Window 2, OpenClaw 2026.9.7:**
- `OPENCLAW_PACKAGE=openclaw@2026.9.7 bash ops/upgrade_openclaw.sh`: the pre-flight passed, then backups, install, plugins update (brave, mistral and slack to 2026.9.7), doctor, TOOLS.md restore, settle, key sync (3 NVIDIA plus OpenAI), all 15 patches in 15 files, skills, verify, restart.
- Timing: the script started at 22:24:36Z. The gateway stopped at 22:26:08Z, started at 22:38:15Z and was ready at 22:39:28Z.
  - Aura was offline for 13 min 20 s, longer than the plan's "few minutes"; npm install, plugins update and doctor took most of it.
  - The first start took 73 s, migration included.
- **Migration:** it imported 107,415 events from 5,773 JSONL-era sessions (February to September 4) into `transcript_events`, with their original `created_at`, and compressed 23,265 of the resulting 122,963 events.

**Follow-ups found on production, with Kirill's approval where they changed production:**
1. **Upgrade ordering bug, fixed before window 2.** `upgrade_openclaw.sh` verified before linking skills. A fresh npm install drops the MoltMarket link, so the verifier would have failed with the gateway already stopped. Skills are now linked first. The old verifier failed the same way.
2. **Usage double count, prevented.** The imported history sits at rowids after the usage scrape's cursor, and the scrape deduplicates only the last 10,000 message IDs, so its next run would have re-counted months of turns.
   - Fix: the scrape now skips events created more than a day before its previous run.
   - Proven on production: the first scrape took exactly the 147 real pre-upgrade events (rowids 24,765 to 24,911, 28 turns) and skipped the import.
   - Test: `test_history_imported_at_new_rowids_is_not_counted_again`. The 24-hour transcript usage was unaffected (539 events before and after).
3. **LangSearch failed to load.** 2026.9.7 loads a captured copy of each plugin (`tmp/plugin-captures/…`), and langsearch required `../aura_web/lib/client.js` by relative path.
   - Fix: it falls back to the file under the state directory.
   - Tests: `tests/node/langsearch.test.js`, which fails without the fix.
   - Deploy: synced to the live plugin directory, followed by a gateway restart (ready in 118 s).
4. **Utility-model calls turned off.** Unset, 2026.9.7 derives `gpt-5.6-luna` for activity recaps (which send transcript excerpts), progress narration and titles. Kirill turned them off.
   - `apply_openclaw_policy` sets `agents.defaults.utilityModel: ""`, and the verifier fails when it is not "".
   - Since that restart, the journal shows no `gpt-5.6-luna` calls.
5. **A probe blind spot.** The plugin probe read the `openclaw` CLI's list, which loads plugins in its own process and showed langsearch as loaded. It now reads the running gateway's journal: no "failed during load", and every local plugin, plus Slack when enabled, in its "http server listening" line. Staging's line had lacked langsearch too.
6. **Docs:**
   - `MIN_NODE` is 24.16.0, and CI runs `node-version: "24"`.
   - The README (prerequisites, rollback command) and ARCHITECTURE (version, utility model, patch rows, upgrade checklist with rehearsal, probes and rollback) are updated.
   - `docs/runbooks/openclaw-update.md` is rewritten: rehearse, upgrade, check, roll back.
   - Rule item 5 names the rollback script, and both copies are identical.

**Deviations and why:**
- **Window 2 was longer than planned:** Aura was offline 13 min 20 s.
- **Unplanned fixes:** items 1 to 4 above were fixes the cutover itself surfaced.
- **Rollback script:** written and rehearsed in step 5; production never needed it.
- **Left on disk:** the staging tree and the `/opt` Node tarball stay until Kirill decides on them. The staging copy holds production data and the config's inline secrets.

**Verification:**
- **After window 1** (Node 24, OpenClaw 2026.9.1):
  - Probes 8/8 (settle is skipped on production).
  - Intake canary PASS (8 s), readiness 38/1/0.
  - The kairos daemons run on v24.21.0, and cron has run every 5 minutes since the switch with nothing in its log.
- **After window 2** (2026.9.7):
  - Probes 8/8: the hook run replied in 10.1 s with the marker turn compressed; abort landed at 3.3 s; the 40 most recent sessions (777 lines) read back unchanged.
  - Verifier passed, intake canary PASS (12 s), readiness 38/1/0.
- **After the follow-ups:**
  - Probes 8/8 with the gateway-side plugin check: aura_web, langsearch, memory-core, openai, rmp_adapter and slack loaded, with no load failures.
  - Verifier "OK: utility-model route off".
  - Health canary `ops/canary.sh` CANARY OK in 15 s.
- **Real DM round trip:** Kirill DMed Aura and she answered normally. Task `e31c6311`: created at 23:14:10Z, the evaluator accepted it, and `slack.delivered` followed at 23:14:47Z, 37 s end to end.
- **Tests:** full suite 832 passed, 4 skipped (284 s); node 22/22.

---

## Step 7 — Claude Code runner

**Date:** 2026-10-01.

**Research:**
- `code.claude.com/docs/en/headless`:
  - SIGTERM exits 143 and records no result. SIGINT ends the turn.
  - `--permission-prompts none` (2.1.259 and later) removes the tools that need a person.
  - `system/api_retry` fields: `attempt`, `max_retries`, `retry_delay_ms`, `error_status`, `error` (12 categories).
  - `system/init` metadata; `--resume <session_id>` works from any directory; `--json-schema` yields `structured_output`.
- `code.claude.com/docs/en/agent-sdk/typescript`:
  - the result's two variants: success, and `error_max_turns`, `error_during_execution`, `error_max_budget_usd`, `error_max_structured_output_retries`;
  - `rate_limit_event`, `permission_denied`, `compact_boundary` and `ModelUsage`.

**Recorded fixtures:** `tests/fixtures/claude_streams/record.py` runs the pinned CLI as `aura-coder` in hardened units, the way the runner does, and keeps each stream with its systemd exit line. Scenarios: `success_readonly`, `edit_and_test`, `max_turns`, `auth_failure`, `unknown_model`, `interrupted`, `resume_first` and `resumed`, plus `synthetic_usage_limit`, which can't be recorded on demand. What the recordings showed:
- **API failures report "success":** an auth failure (401) and an unknown model (404) both end in a result with subtype `success` and `is_error` true (`terminal_reason: api_error`), with exit code 1.
- **A stop leaves no result:** SIGINT ends the run with exit 0 and no result event.
- **Edits through Bash:** Claude fixed the bug with `sed -i` in Bash, not the Edit tool. So changed files come from git (step 8), not from tool uses.
- **Structured report:** it arrives as a `StructuredOutput` tool call.
- **Rate limits:** `rate_limit_event` carries `rateLimitType`, `resetsAt` and `unifiedWindows`.
- **Resumed sessions:** `modelUsage` and `total_cost_usd` cover the whole session, while `usage` covers only the latest run.

**What changed:**
- `app/coding/stream.py` (new) parses the stream incrementally into a `StreamState`:
  - session, model and version;
  - commands, files edited and read, and tool counts (the report tool excluded);
  - tool errors, assistant errors and API retries;
  - the latest rate limit, normalised to epoch seconds;
  - permission denials, compactions, background tasks, the result, and milestones for notices.

  `outcome()` classifies a run as `success`, `max_turns`, `usage_limit`, `auth_failed`, `api_error`, `error`, `stopped`, `timeout`, `no_result` or `running`. Usage limits and auth failures are told apart by error fields, never by `subtype`. `usage_summary()` gives tokens and cost.
- `app/coding/runner.py` (new):
  - `claude_argv`: `-p`, stream-json, `--model`/`--fallback-model`/`--max-turns` from settings, `bypassPermissions`, `--permission-prompts none`, `--append-system-prompt`, `--json-schema` and `--resume`.
  - `start`: unit `aura-claude-<task>-<n>`, step 2's hardening plus `TimeoutStopSec=15`. Stdout goes through `StandardOutput=file:` into the root-only `/srv/aura-code/runs/<task>/<n>/`, and the exit is recorded by a root `ExecStopPost` (`$SERVICE_RESULT $EXIT_CODE $EXIT_STATUS`), so the run can write neither. `meta.json` stores the prompt's hash and length, not its text. It is idempotent: while the unit runs, or once it has ended, it returns the existing run, so an activity retry reattaches.
  - `read_events(run, offset)`: complete lines only.
  - `stop`: records the stop first, sends SIGINT, waits 10 s, then `systemctl stop`, which kills the cgroup.
  - `finish`: the outcome.
  - `record_usage`: books the run's tokens once, from `usage` only, under source `claude_code` and profile `claude_code:subscription`. That source is not in `DIRECT_SOURCES`, so it stays outside the OpenClaw prompt budget.
  - `resume_at`: the usage limit's reset time plus 60 s.
- Supporting changes: `app/coding/units.py` gains `RUNS_DIR`, `ops/setup_aura_coder.sh` creates `/srv/aura-code/runs` (root, 700), and `app/llm/usage_monitor.py` gains the `claude_code` source.
- Tests:
  - `tests/test_coding_stream.py` (19): every recording ends as it did.
  - `tests/test_coding_runner.py` (15), with fake `systemd-run`, `systemctl` and `claude` in `tests/fakes/coding`. The fakes enforce `RuntimeMaxSec`, run `ExecStopPost` with systemd's variables, and replay recordings. Covered: argv, a full run, reattaching by offset with no lost or repeated line, a half-written line, idempotent start, stop within seconds, stop escalation when SIGINT is ignored, timeout, every recorded ending, usage booked once, a resumed run booking only its own tokens, and the usage-limit resume time.

**Deviations and why:**
- **The parser subagent started late.** It was launched on Grok 4.7 extra high, as the plan says, but didn't start for about 20 minutes. I wrote my own parser meanwhile. When its version landed, I kept it, after review, as the plan intends. I stopped it before it wrote tests, and kept my tests, which pass against its parser. It found that resumed runs report cumulative `modelUsage`, so `record_usage` now books `usage` only, and a test covers it.
- **Pausing at the usage limit lands in step 10.** The plan describes pausing until `resetsAt`, telling Kirill and resuming the same session. The runner provides the pieces (the `usage_limit` outcome, `resume_at`, `--resume`). The timer and the notice belong to the coding workflow, and its harness test covers them there.

**Verification:**
- **Tests:** 65 coding and usage tests, then the full suite: 866 passed, 4 skipped (262 s). Node 22/22.
- **Live read-only run through the real runner:** the unit `aura-claude-live-check-read-1` ended `success exited 0`.
  - The report was `{phrase: KESTREL-9}`, in 3 turns, at an estimated $0.128.
  - 30,512 tokens were booked once; the second call was a no-op.
  - The stream and exit files are root-owned with mode 0600.
  - **Reattach:** the script that started the run died. A new `start()` returned the finished run rather than launching another, and tailing from offset 0 read all 7 events.
- **Live stop:** a running Bash loop was stopped by SIGINT in 0.3 s. The unit went inactive, no `aura-coder` process was left (3 before the stop), and the outcome was `stopped`.
  - Hardening on the live unit: `User=aura-coder`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ProtectProc=invisible`, `NoNewPrivileges`, a 1 h 30 min runtime cap, a 15 s stop timeout, 3 GiB of memory and 1,024 tasks.

---

## Step 8 — Workspaces, repositories, independent verification

**Date:** 2026-10-01.

**What changed:**
- **Registry** (`coding.repositories` in `app/config.py`):
  - `rmp` is cloned from the live repo at `/root/.openclaw/rmp`. It is tested with the full suite (through the shared venv) and `node --test tests/node/*.test.js`, and deploys to `self`.
  - `agentic-design` (`npm ci`, `npm test`), `cursor-dual-agent-loop` (a job venv, `pip install -e .`, pytest) and `cyber-ai-team` (a job venv, the backend requirements, `pytest backend/tests`) deploy as a PR.
  - Also new: `verify_timeout_sec` 1800, `job_retention_days` 14 and `diff_limit_chars` 200,000.
- `app/coding/workspace.py` (new):
  - **Preparing a job:** root clones the trusted source with `--no-hardlinks` into `/srv/aura-code/jobs/<task>`. A hardlinked object would change owner in the live repo too. The source is the live repo, or a root-only mirror under `/srv/aura-code/repos` fetched through a temporary `GIT_ASKPASS` that reads the token file at use, so the token never appears in a URL or config.
  - The checkout gets the branch `aura/<task8>-<slug>`, the identity "Aura (Claude Code)" and excludes for `.aura/`, `.venv/`, `node_modules/` and caches. It is then handed to `aura-coder` with mode 0700. A bare review repository at the same base goes to `/srv/aura-code/review/<task>.git` (root-only), and `job.json` goes to the root-only runs directory. Preparing is idempotent.
  - **Collecting:** root runs no git in the checkout, because a planted `.git/config` (fsmonitor, textconv or filter driver) or hook would run as root or misreport the diff.
    - Inside a hardened unit, `aura-coder` commits leftovers with the Aura identity, with hooks and fsmonitor off, and exports `HEAD ^base` as a bundle.
    - Root copies the bundle into the runs directory, then runs `bundle verify` and fetches it into the review repository.
    - Root refuses work that no longer builds on its base.
    - It reads commits, the diffstat, the bounded diff (`--no-textconv --no-ext-diff`), changed paths, changed tests and dependency files there.
  - **The secret scan** reads added lines for key and token patterns and for this host's own secret values, from the env files, the GitHub token and the secrets inside `openclaw.json` and `settings.json`. Findings block a deploy (step 11).
  - **Retention:** `prune(days)` removes checkouts and review repositories past retention unless one of their runs is live. The reconciler loop calls it hourly.
- `app/coding/verify.py` (new):
  - **The run:** the repository's setup and test commands, each as `aura-coder` in its own hardened unit (writable: the checkout, its home and the cache). Output goes to root-owned files, and the exit codes recorded by systemd are the evidence. Counts are parsed from pytest and node, but only for information, because the job can change its own tests.
  - **Shared venv:** `ensure_shared_venv` builds `/srv/aura-code/venvs/<repo>` from the trusted source's requirements only. A job whose `requirements.txt` differs builds its own venv in the sandbox.
  - **Test cache:** `seed_test_cache` places Temporal's 84 MB test server in `aura-coder`'s `TMPDIR` (`/srv/aura-code/cache/tmp`), since units have a private `/tmp`.
- `app/llm/quota_broker.py`: an unreadable OpenClaw env file now reads as "no keys". The sandbox hides `/etc/openclaw`, and `Path.is_file()` raises `PermissionError` on a hidden path. This fixed 10 failures in the first live run.
- Tests:
  - `tests/test_coding_workspace.py` (8): branch, base and identity on prepare, and idempotence; an empty collection; Claude's commit plus leftovers collected from the review repository, with changed tests and dependencies; a planted fsmonitor and textconv neither run nor fake the diff; work rewritten off its base is refused; the secret scan; retention keeps a job with a live run; the GitHub mirror is fetched without storing the token.
  - `tests/test_coding_verify.py` (7): a passing suite; a failing suite that prints a fake "2 passed"; a failed setup stops the run; the shared venv is used only for matching requirements; it is rebuilt when they change; the Temporal seed; summary parsing.
  - `test_quota_broker.py` gains the unreadable-env case.
  - The fake `systemd-run` gains `--wait` and `StandardError=inherit`.

**Deviations and why:**
- **The review-repository design is stricter than the plan's "collects commits, a diffstat, a bounded diff".** Reading the diff in Claude's own checkout would let its git config misreport the diff, or run code as root. Step 11's deploy takes its commit from the review repository too.
- **I wrote the workspace and verification code myself**, not through a subagent, after step 7's subagent started 20 minutes late.
- **The per-repository commands for the three PR repositories** are declared from their manifests and not yet run here. Step 14's PR acceptance runs one of them.

**Verification:**
- **Tests:** 66 coding tests, plus the affected modules (56).
- **Shared venv:** built from the live `requirements.txt` in 62 s. It is root-owned and read-only for `aura-coder`, which can import pytest 8.3.5 and temporalio 1.23.0 from it. The test server is seeded.
- **Live: the RMP suite as `aura-coder` from a job checkout** of this branch, in hardened units:
  - First run: 870 passed, 10 failed, all of them the `PermissionError` above.
  - After the fix: 881 passed, 5 skipped, exit 0 (276 s). Node 21/22 passed, with the live-plugin drift check skipped because `/root` is hidden.
- **Live collection:** a change made as `aura-coder` became one commit by "Aura (Claude Code)" (`docs/CODING_CAPABILITY_PROGRESS.md` M, `leaked.py` A, 2 insertions). The secret scan flagged the planted `ghp_…` string in `leaked.py`. The head was read from the review repository.
- **Cleanup:** the scratch job, review repository and run records were removed afterwards.

---

## Step 9 — Approval and evidence hardening

**Date:** 2026-10-01.

**What changed:**
- `app/workflows/approval.py` (new): `gate_decision(signalled)` reads only Kirill's words (`user_words`), never the catch-up brief before them.
  - **Approves:** a whole message `approve`, `approved` or `deploy`.
  - **Stops:** a whole-message stop (the existing `is_whole_message_stop`: stop, cancel, abort, halt, "please stop"), or `reject` or `deny`.
  - **Anything else** is "other". That includes "yes", "ok", "go ahead" and "approve, but…".
  - It also defines `REMINDER_AFTER` (12 h) and `CLOSE_AFTER` (7 days).
- **The catalog gate** (`app/workflows/catalog_task.py`) decides with `gate_decision`. It sends one reminder after 12 hours, and closes the task as `cancelled` (terminal) after 7 days with "nothing was done". `_stop_task` takes a status. The coding gate (step 10) uses the same function.
- **Provenance:**
  - The plugin's inbound record keeps the Slack `senderId` and `timestamp` (from the 2026.9.7 SDK's inbound facts), and `POST /tasks` sends them as `slack_user_id` and `slack_event_ts`.
  - `TaskRequest` takes both. `_slack_context` (new tasks) and `_slack_meta` (attached messages) store them as `meta.slack.user_id` and `event_ts`.
  - The new activity `confirm_approval_provenance` (registered in `worker.py`) accepts only a Slack message from the configured owner, recorded after the gate opened, whose own words approve. An API signal, another sender, an earlier message or a conditional "approve, but…" is refused, and an `approval.refused` event is recorded.
- **The evaluator:**
  - `format_external_evidence` writes up a Claude Code run's outcome, commands and edits, RMP's own test run, the commits, the diffstat and secret findings.
  - `verify_response_quality` passes `external_evidence` into a new prompt section, `EXTERNAL EVIDENCE (recorded by RMP, not by Aura)`. The prompt rule: code work is done only as far as that evidence shows it, and tests pass only when RMP's own run passed.
  - The OpenClaw transcript trace stays as it was. Tasks without evidence show "(none)".
- Tests:
  - `tests/test_approval_gate.py` (21): an 18-case decision table, including briefs that mention "stop" and "ok", plus procurement on the time-skipping server:
    - a brief doesn't decide, an unclear reply gets "not recognised", and "approve" lets the purchase run;
    - a whole-message stop stops at the gate;
    - no reminder at 11 h, exactly one at 13 h, and `cancelled` at 7 days.
  - `tests/test_approval_provenance.py` (6): the owner's approval after the gate opened is confirmed; another sender, before the gate, an API signal and a conditional approval are refused and recorded; the attach path keeps the sender.
  - `tests/test_process_evaluator.py` (+2): with and without evidence.
  - Node: the sender and event time reach `POST /tasks`.
  - `tests/test_ingress_identity.py`: the Slack context carries the sender.

**Deviations:**
- **A narrower API approve signal in the catalog gate:** it still works for the existing non-deploy catalog flows (procurement, browser automation). The provenance check applies before deploys, as the plan says, so the coding gate (step 10) requires it.
- **Deploy:** the plugin change needed a gateway restart, taken at an idle moment. There were no open process runs, so no in-flight catalog workflow could replay into the changed gate.

**Verification:**
- **Tests:** full suite 911 passed, 4 skipped. The one failure, an exact-dict test, was then updated to cover the sender, and its module passes. 79 gate, evaluator and catalog tests; node 23/23.
- **Deploy:** the plugin was synced (live == repo), and the gateway was ready 108 s after the restart. The plugin probe shows all six runtime plugins loaded with no load failures. The API and worker reloaded without errors.
- **Live provenance:** Kirill sent a DM ("Quick check, please reply OK"), and its task message records `user_id=U0AELFYTLKS` (the configured owner) and `event_ts=1790818985363`. The message before the deploy had neither.

---

## Step 10 — Coding workflow

**Date:** 2026-10-02.

**What changed:**
- `app/workflows/coding_task.py` (new): `CodingTaskWorkflow`, registered in `worker.py`. `start_task_workflow` starts it for `coding_task`, given as the process type, task type or catalog type (the catalog entry arrives in step 12).
  - **Slot:** one coding job at a time. A busy slot gets one queue notice, and the task waits as `blocked`/`queued`, polling every 2 minutes. The workflow releases the slot when it ends, and a holder whose task has ended loses it.
  - **Brief:** Aura writes it as JSON: repo, title, goal, acceptance criteria, constraints and questions. Questions go to Kirill (`pending_user_input`, one reminder after 12 h, closed after 7 days), for up to two rounds. An unknown repository becomes a question.
  - **Workspace:** step 8's `prepare`, the shared venv and the test-server seed. Kirill then gets a "Starting on <repo>" notice naming the branch.
  - **Rounds:** Claude Code runs (a rework resumes its session), then RMP collects the work and runs the tests. Aura reviews only when RMP's record shows commits, passing tests and no secret. She reviews in a fresh session per round (`__r<round>`) that reads the diff from disk. The evaluator judges her reply with the external evidence only when she says ready. Failing tests, her feedback, the evaluator's issues and Kirill's new messages go into the next round. `max_rounds` (3) applies per batch of rounds.
  - **Usage limit:** the round pauses (`blocked`/`paused`) until the reset plus a minute, with a notice. Then the same session continues with a continue prompt. A stop during the pause ends the task.
  - **Gate:** applies to every repository with a deploy target, `self` and `pr`. The card holds Aura's judged summary, the commits and head, RMP's test result, the diffstat, the services a deploy restarts (`self` only, from `app/coding/deploy.py`) and dependency changes.
    - A change that is still not ready after the last round gets a card listing the problems, and an approve is refused.
    - Replies are read with `gate_decision`. A batch of replies containing any change request is one change request (joined), which starts a new batch of rounds in the same session. Only a batch of approvals reaches `confirm_approval_provenance`. A refused one (such as an API approve) gets a notice, and the gate stays open.
  - **Ship:** the `deploy_coding_change` activity (step 11), then Aura's final reply, judged with the deploy result as evidence. If no draft passes in two tries, RMP's own record of what shipped is sent instead.
  - **Stop:** `cancel`, or a whole-message stop in Kirill's own words (`user_words`). The workflow first runs `stop_coding_units`, which records the run's stop, sends SIGINT and then stops the unit. Then it cancels the activity and waits for its cleanup (`WAIT_CANCELLATION_COMPLETED`). The checkout is kept, and the notice names the branch.
  - **Failures** close the task as failed with a notice, keeping the checkout. They are a runner outcome of `auth_failed`, `api_error`, `timeout`, `no_result` or a stop from outside, or a failed preparation or verification.
- `app/activities/coding_activities.py` (new): `coding_settings`, `acquire_coding_slot` and `release_coding_slot` (a locked file in the runs directory), `draft_coding_brief` (asks once more after unreadable output), `prepare_coding_workspace`, `run_claude_round`, `verify_coding_round`, `review_coding_round` and `stop_coding_units`.
  - `run_claude_round` tails the stream and heartbeats the offset. It touches liveness every 3 minutes and sends a milestone notice at most every 15. A retry reattaches (the runner's start is idempotent) and rebuilds the run's record from the whole stream. Only a cancel the workflow asked for (`cancellation_details().cancel_requested`) stops the unit; a worker shutdown or a missed heartbeat leaves it running.
  - `verify_coding_round` stops the round's leftover units on a retry. It keeps the diff on disk (`diff-<run>.patch`) and returns RMP's collection without it, plus the test run. Work RMP cannot collect comes back as `error`, which becomes feedback for Claude.
- `app/coding/prompts.py` (new): the brief, system, rework, change-request, review and final prompts, `REPORT_SCHEMA`, and parsers that fail closed. `app/coding/deploy.py` (new): `restarts_for(paths)`.
- **The reconciler and liveness:**
  - **Orphans:** a new pass closes a coding task whose workflow is gone. It stops the task's units, marks it failed, sends a notice and records `reconciler.coding_orphan_closed`.
  - **No re-judging:** the orphaned-reply recovery never re-judges a coding task.
  - **Stuck repair:** it skips a coding task while one of its units is live (then the worker is down, not the run), and stops the units when it terminates the workflow.
  - **Terminate:** `terminate_task_workflow` (supersede, rebuild) stops the task's coding units too.
  - **Janitor:** it already recognises `workflow-<task>`, and the coding workflow has no children.
- The evaluator's external evidence gains the deploy result.
- Tests:
  - `tests/test_coding_workflow.py` (17), with scripted activities on the time-skipping server: approve and deploy; rework from failing tests and from review feedback; Kirill's messages reaching the next round; a stop mid-run (units stopped within seconds, before the activity is cancelled); a stop at the gate; a change request; an approve sent with a change request; a refused API approve; the usage-limit pause, and a stop during it; a runner failure; the queue; brief questions; not ready after the last round; a secret in the diff; a PR repository.
  - In the same file, a worker restart mid-run: the real round activity on a scripted runner. The unit was started once and never stopped, the retry attached while it was still running, and the record covers the whole run.
  - `tests/test_coding_activities.py` (9): the slot; stopping a task's units with fake systemd; only a requested cancel stops a run; verification keeps the diff on disk; uncollectable work; the brief retry.
  - `tests/test_coding_prompts.py` (20), `tests/test_reconciler_coding.py` (7), and five recorded histories in `tests/test_workflow_replay.py`. Also routing, terminate and janitor cases, and the evaluator's deploy line. The fake `systemctl` gains `list-units`.

**Deviations and why:**
- **Review and judgment only where they can matter:** a round that RMP's own record already fails goes straight back to Claude, without Aura's review or the evaluator. This saves two LLM turns per failing round. Everything that reaches Kirill is still judged.
- **The gate covers PR repositories too:** the plan's architecture diagram puts the approval card before the split by target.
- **The deploy activity is called by name** until step 11 adds it. Nothing routes to coding tasks before step 12.
- **The reconciler's query:** the first version used `SELECT DISTINCT` over task rows. Postgres refuses that because of the JSON column ("could not identify an equality operator for type json"), confirmed read-only on the live database. The pass uses a subquery instead. SQLite tests cannot catch this, so the live query was run read-only before the deploy.
- **The reattach test uses a real dev server** (`start_local`), because Temporal's time-skipping server never retries an attempt whose worker shut down. On a real server the retry came about 2 s after the old worker's shutdown (measured), not after the 2-minute heartbeat timeout.

**Verification:**
- **Tests:** full suite 969 passed, 4 skipped; node 23/23. A deliberately nondeterministic edit makes the replay test fail.
- **SDK behaviour, confirmed by experiment on temporalio 1.23:**
  - heartbeat details survive a retry;
  - a workflow cancel arrives with `cancel_requested`, and a worker shutdown with `worker_shutdown`;
  - with the default cancellation type, the workflow moves on before the activity's cleanup runs.
- **Live probe on this host** (real activities, no Slack):
  - a checkout of RMP at `49544ed`;
  - a read-only Claude Code round: success in 15 s, 3 turns;
  - a second round, stopped 0.3 s after the cancel, with outcome `stopped`;
  - RMP's verification as `aura-coder`: no commits, 910 passed, 5 skipped, node 22 passed and 1 skipped, in 315 s.

  The probe's files were removed.
- **Deploy:** `main` was fast-forwarded to `4a2e8c2` with no active tasks. The code watcher restarted the API and worker within a minute, with no errors. Readiness is 38/1/0, the gateway's `/readyz` returns 200, and `ops/canary.sh` gives CANARY OK. The new reconciler pass runs without warnings.

---

## Step 11 — Deploy pipelines

**Date:** 2026-10-02.

**What changed:**
- **The deploy activity** (`deploy_coding_change`, in the worker):
  - **Other repositories:** it pushes the approved head from the review repository as the job's branch, through the askpass that reads the token file. It opens the PR with `gh pr create`, giving `GH_TOKEN` only to that command, with Aura's judged summary and RMP's test result as the body, and returns the link.
  - **Aura's own code:**
    1. It fetches the approved commit from the review repository into the live repo (`refs/aura/<task>`). If `main` is no longer its ancestor, it returns `needs_rebase`.
    2. It runs the full suite on exactly that commit, in a clean checkout owned by `aura-coder` in hardened units, never as root.
    3. It writes the deploy spec and starts the detached root unit `aura-deploy-<task>` (`ops/coding_deploy.py`, with the API's environment files). A retried activity that finds the spec does not start a second deploy.
- **The workflow:**
  - A hand-off ends the run as `deploying` (the task's status too), keeping the coding slot.
  - `needs_rebase` gives the job today's `main`: a bundle in a root-owned, readable directory (`/srv/aura-code/bundles/<task>`, never inside the checkout) and the review repository's new base. Claude rebases in the same session, and the round goes through review, the evaluator and a new card, because the rebased change is approved again.
  - A run started with `report` (by the deploy unit) only writes and judges the final reply, with the deploy's result as evidence, then releases the slot.
- **The deploy unit** (`app/coding/deploy.py` `self_deploy`, run by `ops/coding_deploy.py`):
  - It imports everything it uses before `main` moves. It holds `/run/rmp-code-reload.lock`, so the code watcher cannot restart anything during the deploy.
  - It waits until no other user task is active (a `deploying` task does not count; at most 2 hours, then `postponed` with `main` unchanged). It refuses if `main` moved meanwhile.
  - It checks each touched mirror for drift: a live file that matches no committed version of its path means a hand edit, which blocks the deploy before `main` moves. A live copy that is only behind is updated.
  - It takes a readiness baseline, fast-forwards `main` and pushes it.
  - It writes the commit's version of changed mirror files and deletes removed ones: `plugins/`, `web-stack/`, `systemd/` and `.cursor/rules/`. It runs `pip install -r` when `requirements.txt` changed and `daemon-reload` for units, then restarts only what changed (`restarts_for`).
  - **Checks** (`app/coding/deploy_checks.py`):
    - RMP's `/health`;
    - the gateway's `/readyz` when it restarted;
    - no readiness check failing that was not failing before;
    - the canary, whose "CANARY OK" is required (a skip while Kirill is busy is retried, then reported as skipped).
  - **On failure** (or an exception while applying), it commits a revert of the range, restores the mirrors, restarts the same services, checks again and pushes the revert.
  - It records the result as a `coding.deploy` event and in the task's context, then starts a fresh run of `workflow-<task>`, which replies on the new code. Nothing replays the old history across the change, even when the change touches the coding workflow itself.
- **The reconciler** leaves a `deploying` task to its deploy unit while the unit lives or within 4 hours. A deploy that never reported back is closed with "please check main and the deploy log".
- Tests:
  - `tests/test_coding_deploy.py` (13), against a real live repo, its GitHub remote, the review repository and mirrors, with a recording host:
    - fast-forward, push, sync and restarting only what changed;
    - pip and daemon-reload before the restart;
    - a hand-edited mirror blocks before `main` moves, while a mirror that is only behind is updated;
    - the full rollback: the revert's tree equals the old tree, mirrors are restored, restarted twice, and the revert pushed;
    - the idle wait and postponing, and `main` moving meanwhile;
    - fast-forward or rebase, and the rebase bundle;
    - the PR with the token given to `gh` only;
    - one hand-off as root, with a failed start forgotten;
    - the exact-commit suite on a clean checkout;
    - the deploy unit holding the lock, recording the result and starting the reply run.
  - The harness (+2): the hand-off keeps the slot and the report run replies, and a rebase round leads to a new card. Reconciler (+1): a deploying task. A sixth recorded history (`rebase_then_approve`); all six were re-recorded on the final code.

**Deviations and why:**
- **Fast-forward and push happen in the deploy unit, under the code-reload lock, not before the hand-off.** If `main` moved first, the watcher would restart the worker under the still-running deploy activity, and the worker would come back on the new code.
- **The exact-commit suite runs as `aura-coder` in a clean checkout,** not in a root-owned RMP worktree, so code written by Claude never runs as root.
- **The reply comes from a fresh run of `workflow-<task>`,** started by the deploy unit, not from the original run waiting through the restart. That is what makes the deploy safe for changes to the coding workflow itself.
- **The drift rule tolerates a mirror that is only behind:** the live web stack has one file older than the repo (the User-Agent string from before the repository rename in `371c87d`). A strict comparison would block every web-stack deploy over a harmless lag.
- **A failed rebase does not loop silently:** the rebased change is reviewed, judged and approved again, because it is no longer the commit Kirill approved.

**Verification:**
- **Tests:** full suite 985 passed, 4 skipped; node 23/23.
- **Live, nothing shipped:**
  - drift on the four real mirrors: none (the web stack's older file counts as behind);
  - `deploy_checks.run` on production: health, readiness (baseline empty) and a real canary passed in 27 s;
  - the exact-commit suite on `main` (`d45aed1`) as `aura-coder` in a clean checkout: 968 passed, 5 skipped, node 22 passed and 1 skipped (362 s);
  - a root transient unit with the deploy unit's properties ran as uid 0 with the database settings from the environment files, importing from the live repo.

  The probe's files were removed. A real self-deploy and a real PR are step 14's acceptance with Kirill.
- **Deploy:** `main` was fast-forwarded to `ec55c06` with no active tasks. The watcher restarted the API and worker (health OK), with no errors. Readiness is 38/1/0, the gateway's `/readyz` returns 200, and the canary gives CANARY OK.
- **Finding for Kirill:** the live `web-stack/backends/app/adapters/crawl4ai_adapter.py` still has the old User-Agent (`.../aura`). The first coding deploy that touches `web-stack/` brings it up to date.

---

## Step 12 — Intake and Aura integration

**Date:** 2026-10-02.

**What changed:**
- **The catalog:** `CODING_TASK` (`coding_task`, "Coding task (Claude Code)") replaces `TOOL_SELF_UPGRADE`.
  - `tool_self_upgrade`, `self_upgrade` and `capability_upgrade` are its aliases.
  - It has no catalog steps: `start_task_workflow` starts `CodingTaskWorkflow` for it.
  - Its patterns stay advisory (soft candidates for the intake LLM): change requests, PRs, "upgrade yourself" and new tools. Negatives cover awareness ("are you aware", "can you code"), questions ("how does", "what does … do", "explain") and "I just added".
  - Its success criteria require Kirill's approval, passing tests and verification.
- **Removed:** `ops/controlled_capability_restart.sh` and `ops/verify_capability_upgrade.sh`, with the old template's keyword check in `app/evidence.py`; coding tasks are judged on RMP's recorded evidence instead. The Slack-sockets runbook now names `systemctl restart openclaw-gateway` (only when idle).
- **Intake prompt:** `coding_task` is set when Kirill asks for a change to code in Aura's own code or one of his repositories. It is never set for awareness, questions about code or a review without a change.
- **Jev:** `CATALOG_RUBRIC` gains `coding_task` ("Change code in Aura's own code or one of Kirill's repositories… Not a question about code or about what Aura can do."). `jev_intake_eval.jsonl` relabels the PDF-tool request and gains 8 cases:
  - three coding requests: an own-code fix, a PR in agentic-design, a test plus fix;
  - two awareness messages;
  - two questions about code;
  - a review without changes.
- **Workspace:** a "Coding tasks (Claude Code)" section replaces the self-upgrade section in `/root/.openclaw/workspace/TOOLS.md` and its `AGENTS.md` copy (identical).
  - **Aura's part:** the brief as JSON, a review against RMP's evidence with the verdict JSON, and the final reply from RMP's record.
  - **Never:** run `claude`, or edit, commit, push or restart live code.
  - **How it ships:** her own code deploys through RMP, and other repositories get a PR.
- `CLAUDE.md` (new, repo root): where things are, test commands (including the replay test for workflow changes), invariants (the Slack path, judged replies, deterministic workflows, no keyword routing, no secrets, the rule copy) and commit conventions.
- Tests:
  - the catalog: coding requests resolve, aliases, no steps and the gates, awareness and questions about code are not catalog;
  - intake adjudication: the hint is accepted and the alias normalized;
  - evidence: no keyword gate for coding tasks;
  - the relabelled intake fixtures;
  - a whole-path scenario (`tests/test_whole_path.py`): a DM whose intake answer assigns `coding_task` starts `CodingTaskWorkflow` through the real API, intake and policy. Its start notice and PR card reach Slack through real delivery, the evaluator judges with RMP's evidence, and Kirill's stop ends it.

**Deviations:**
- **The paid Jev evaluation (`--live`) was not run;** the offline harness validates all 59 cases. The intake LLM was checked live instead (below). A live Jev run costs TypeSafe credits and is worth doing once before Jev's catalog answers are trusted for `coding_task`.

**Verification:**
- **Tests:** full suite 987 passed, 4 skipped; node 23/23. `ops/jev_eval.py --intake`: 59 cases validated, no model called.
- **Live intake classification** (new prompt and catalog, the production intake LLM; no task, no Slack):
  - "In agentic-design, add a dark mode toggle… and open a PR" became `coding_task`, structured work;
  - "Please fix that in your code" (reconciler notices) became `coding_task`, structured work;
  - "Did you know you can write code with Claude Code now?" had no catalog and stayed conversational;
  - "How does your reconciler decide that a task is stuck?" had no catalog.
- **Deploy:** `main` was fast-forwarded to `98cf9ec` with no active tasks. The watcher restarted the API and worker (health OK), with no errors. Readiness is 38/1/0, the gateway's `/readyz` returns 200, and the canary gives CANARY OK. From now on, a coding request in a DM starts a coding task.

---

## Step 13 — Observability, docs, rules

**Date:** 2026-10-02.

**What changed:**
- **Coding API** (behind the API key, like the rest of `/api`):
  - `GET /api/coding/status`: the installed and pinned Claude Code version, days left on the token, the coding slot's holder, live coding units, the repositories and recent jobs.
  - `GET /api/coding/jobs/{task_id}`: the job record, each Claude Code run (meta, exit, outcome, turns, usage, error), each verification (without the output tails), the deploy's commits and the coding events (`approval.confirmed`/`refused`, `coding.deploy`, `coding.reported_by_rmp`, `reconciler.coding_orphan_closed`). A task id that is not a plain identifier gets 404, so the path cannot leave the runs directory.
- **Readiness** (`app/production/coding_readiness.py`, added to the report):
  - `claude_code`: the binary present at the pinned version (read from the versions path it links to, never executed as root), the token present (fail when expired, warn under 30 days), and the last smoke run (warn when missing or failed).
  - `coding_isolation`: the nftables table loaded, every loopback listener covered, managed settings identical to the repo's copy.
  - `coding_jobs`: Claude Code units running past their limit, and job checkouts without a job record (warn).
- **Invariants:**
  - `approved_deploys`: every shipped change (`deployed`, `rolled_back` or `pr_opened`) has an `approval.confirmed` before its `coding.deploy`. `confirm_approval_provenance` now records `approval.confirmed`, and the deploy activity records a PR as `coding.deploy`, as the deploy unit already did for self-deploys.
  - `deploy_verification`: every `deployed` self-deploy records the exact-commit suite (now carried from the activity into the unit's result) and a canary that ran or was skipped.
  - `coding_units`: no `aura-claude-*` unit without a live task.
  - `judged_deliveries` accepts `coding.reported_by_rmp`, the event a coding task records when it completes with RMP's own record because no reply of Aura's passed the evaluator.
- **Docs:**
  - `ARCHITECTURE.md` §5.11 "Coding tasks (Claude Code)": isolation, flow, shipping, durability and observability, plus the two endpoints in §5.7.
  - `docs/CONCEPT_TREE.md`: `CodingTaskWorkflow` among the workflow kinds (the catalog list loses "tool self-upgrade"), and a MUST for coding jobs.
  - `README.md`: a "Coding" guarantee, and monitoring now names the coding checks and the twelve invariants.
- **The rule:** item 6 of `rmp-architecture.mdc` covers coding jobs. The repo copy and `/root/.cursor/rules/` are identical.
- Tests: `tests/test_coding_observability.py` (12):
  - healthy `claude_code`, and five ways it reports what is wrong;
  - isolation failing on each of its three conditions;
  - a stuck unit and an orphan checkout;
  - an unapproved and a late-approved deploy;
  - a self-deploy without a verification record;
  - a Claude Code unit without a live task;
  - the job API, including the path refusal.

**Verification:**
- **Tests:** full suite 999 passed, 4 skipped; node 23/23.
- **Before the deploy,** the three readiness checks on this host: Claude Code 2.1.280 (pinned), the token valid for 363 more days, the smoke run from Sep 30 passed; firewall loaded, every listener covered, managed settings intact; no stuck units or orphan checkouts.
- **Deploy:** `main` was fast-forwarded to `e20d900` with no active tasks. The watcher restarted the API and worker (health OK), with no errors.
  - Readiness is now **44 pass, 1 warn (telemetry), 0 fail**; the six new checks and invariants all pass.
  - `GET /api/coding/status` answers, and an unknown job gets 404.
  - The canary gives CANARY OK.

---

## Step 14 — End-to-end proof, live acceptance, push

**Date:** 2026-10-02.

**Automated proof:**
- **Full suite:** 1004 passed, 4 skipped, with the harnesses and the six recorded histories included; node 23/23.
- `ops/verify_openclaw_patch.sh` passes (patches, model policy, the utility-model route off), and `ops/openclaw_preflight.py openclaw@2026.9.7` passes (Node range, RMP patches, transcript format).

**Fixes the acceptance found** (each deployed before the next attempt):
1. **Relative executables** (`10d3fcb`): systemd refuses a relative executable path, so `.aura/venv/bin/pip` never started (exit "missing"). The fake `systemd-run` accepted relative paths, and step 8's live run used the absolute shared venv, so neither caught it. `verify.commands_for` now resolves them against the checkout. The same commit corrected the other repositories' declared commands, later removed with the registry (fix 5).
2. **Shipping as a round criterion** (`80ab11e`): Kirill asked to "open a PR", my brief made that an acceptance criterion, and my review held 12 rounds back because Claude Code (rightly) cannot push.
   - The brief and review prompts now say that pushing, PRs and deploys are RMP's job after approval.
   - Cards and round notices say what the review found.
   - An approval sent before the card is no longer passed to Claude.
3. **The evaluator** (`a830b95`) also judged rounds against the literal ask and could not see the diff. It now gets a stage note (a round's reply comes before approval; nothing has shipped) and a bounded copy of the diff, read from disk. Replayed on the failed round, it accepted.
4. **The PR push was refused (403):** `git push --dry-run` showed the token can push only to openclaw-jev.
5. **Only Aura's own repository** (`f356adb`): Kirill decided Aura must not touch his other repositories. The registry now holds only `rmp`. Intake, Jev's rubric and fixtures, Aura's `TOOLS.md`/`AGENTS.md` and the docs say a change to another repository is not a coding task. With Kirill's approval, the local mirrors of the three repositories and the four acceptance tasks' checkouts, review copies and run records were deleted. Nothing was ever pushed to them.
6. **A false alarm in `judged_deliveries`** (`276b2e6`): intake's acknowledgement of a message with nothing new to run ("I approve", sent after the deploy had finished) completes a task with RMP's own words. It is now counted like a coding task's shipped record. This was latent since the invariant appeared on Sep 29; today's was the first such acknowledgement.

**Live with Kirill in Slack:**
1. **A stop during a run** (task `9e31b599`): "Starting on cursor-dual-agent-loop…", then Kirill's "stop". The task was `stopped_by_user` 3 s later, the unit was gone, the run's outcome was `stopped`, and the slot was released. Nothing shipped.
2. **A change request at the gate** (task `80e6c732`):
   - The first card came after one round (evaluator accept).
   - Kirill's change request started a new round. The evaluator returned Aura's reply once for an unsupported claim, then accepted, and the second card followed.
   - Kirill's approve was confirmed (`approval.confirmed`, his Slack user, after the gate opened).
   - The PR push was refused (fix 4); Aura's judged final reply said so, and the task closed `failed` with nothing deployed.
   - Earlier attempts (`db383558`, 12 rounds; `0dae0365`) led to fixes 2 and 3.
3. **A self-deploy of Aura's own code** (task `bd56025f`, "Add the current RMP commit (short hash) to the response of GET /api/coding/status, with a test"):
   - Claude Code's commit `cea120b`, by Aura (Claude Code), touched `app/api/server.py` (`rmp_commit` from `git rev-parse --short HEAD`, `null` outside git) and a test. RMP's run passed, and the evaluator accepted the reply.
   - An "approve" Kirill sent before the card was refused by the provenance check, which asked again. The next approve was confirmed.
   - The full suite on the exact commit passed.
   - The deploy unit held the code-reload lock, fast-forwarded `main` (`f356adb..cea120b`), pushed, restarted rmp-api and rmp-worker, and passed health, readiness and the canary, all in 28 s. It then started the reply run, whose judged reply reached Slack, and the task completed.
   - CI on `cea120b`: success (run 36967544033). `GET /api/coding/status` now returns `rmp_commit: cea120b`.

**Deviations:**
- **No PR acceptance:** Kirill ruled his other repositories out of bounds (fix 5), and the token has no write access to them. The PR path stays in the code and its hermetic tests, unused.
- **A stuck watcher:** my monitoring loop during the self-deploy missed the task's completion and waited until Kirill restarted Cursor. The system was not affected: the deploy had finished at 05:08 UTC.

**Final state:**
- **Readiness:** 43 pass, 2 warn, 0 fail. The warnings are telemetry (by design) and `vector_sync` (one registry entry awaiting the nightly reconcile).
- **The coding invariants** pass on real data: `approved_deploys` (1 shipped change, approved), `deploy_verification` (1 self-deploy, verified), `coding_units` (0 orphans).
- **Acceptance targets:**
  - isolation: step 2's probes;
  - no unapproved deploy: the provenance refusal was observed live, and the invariant enforces it;
  - verified deploys with automatic rollback: live for the deploy, hermetic for the rollback;
  - a stop within seconds: 3 s live;
  - 2026.9.7: the probes, canary and a live DM;
  - the full suite and CI green, readiness 0 fail.
