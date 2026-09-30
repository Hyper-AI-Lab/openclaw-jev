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
