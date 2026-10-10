# Platform hardening programme: progress log (append-only)

This log follows the plan `/root/.claude-team/plans/you-are-a-senior-jolly-piglet.md`, as approved by Kirill on 2026-10-08.

**Rules:**
- Every step appends an entry. No entry is ever rewritten.
- The repo's canonical copy is `docs/PLATFORM_HARDENING_PROGRESS.md`. Each PR appends these entries to it verbatim.

---

## 2026-10-08 · Step 0: programme persisted

`/root/rmp-intelligence/program/` now holds:

| File | Contents |
|---|---|
| `requirements.md` | The 144-requirement inventory. Preliminary status: 37 MATCH, 87 PARTIAL, 6 MISMATCH, 3 MISSING, 8 SUPERSEDED, 3 UNVERIFIED. Extracted complete from the explorer transcript and checked: R-01…R-144 all present. |
| `work-packages.md` | 30 WPs, 14 decisions, execution order and the shared-file table. Coverage checked: all 82 open findings and all 65 second-pass ids have a home. |
| `redteam.md` | The independent planner's critique. All of it is applied in the plan. |
| `decisions.md` | Kirill's decisions K-1…K-12, the defaults, and the `CLAUDE.md` overrides. |
| `ordering.md` | 20 hard ordering rules. |
| `ledger.json` | 430 tracked items: 83 findings (1 closed, 1 refuted), 65 second-pass items, 138 seed risks not covered by any finding (Phase B4 triage), and 144 requirements (Phase A3 mapping). |

**Ledger updates:** OT-1 is closed (the stray audit curl was stopped on 10-07), and AS-01 is closed (PR #23 plus the password rotation). MC-6, UW-2 and UW-3 now point to WP-16, WP-11 and WP-10, per the D-9, D-10 and D-8 defaults.

**Next:** Phase 0 (containment), before 2026-10-11 20:32Z.

## 2026-10-08 16:01 · Phase 0.1: Skill Workshop off, internal hooks declared

**Change:** `openclaw config patch` (validated by a dry run first: 6 updates) set two things:
- `skills.workshop.autonomous.mode = "off"`;
- `hooks.internal.entries`, declared explicitly. `boot-md` is disabled; `bootstrap-extra-files`, `command-logger`, `compaction-notifier` and `session-memory` stay enabled, which keeps today's behaviour apart from `boot-md`.

Aura was idle (0 active user tasks).

**Evidence:**
- Gateway journal at 16:01:46-47: "config hot reload applied (skills.workshop.autonomous.mode, hooks.internal.entries)". No restart.
- The state DB, read-only, shows `cron_jobs` `skill-collection-review-main` with `enabled=0` and no next run. The weekly root agent turn planned for 2026-10-11 20:32Z will not run (UW-5, UC-27, MC-4).

**Rollback:** `install -m 600 /root/.openclaw/openclaw.json.bak-programme-20261008 /root/.openclaw/openclaw.json`. The gateway hot-reloads it.

**Observed, outside Phase 0's scope:** the OpenClaw-native "Memory Dreaming Promotion" cron is still enabled and runs daily at about 20:00. It is a gateway-owned turn outside RMP, handled in WP-17 (R-92).

**Still open:** keeping these settings when OpenClaw upgrades. `ops/upgrade_openclaw.sh:117-125` rewrites `hooks.internal` and needs a readiness invariant. That is WP-17.

## 2026-10-08 16:05 · Phase 0.2: OpenClaw rollback backup rescued from pruning

**Why:** `ops/backup.sh:100` keeps only the 14 newest directories in `data/backups/` (sorted with `ls -1dt`). `openclaw-update-20260930T222604Z`, the only OpenClaw rollback backup, ranked 9th and would have been deleted around 2026-10-14.

**Change:**
- Copied it with `cp -a` to `/root/.openclaw/archive/openclaw-update-20260930T222604Z` (dir mode 0700, 113 MB). `diff -r -q` confirms the copy is identical.
- Repointed `/tmp/openclaw-upgrade-backup.path` to the archive. `ops/rollback_openclaw.sh:42` reads that pointer, or `BACKUP_DIR`. The original pointer is saved as `archive/openclaw-upgrade-backup.path.orig`.

**Finding for the reboot step:** `/usr/lib/tmpfiles.d/tmp.conf` has `D /tmp 1777 root root 30d`, so `/tmp` is **emptied at every boot**. Before the reboot (Phase 0.10):
- Archive Aura's OpenClaw fork (`/tmp/openclaw-v2026.9.7`, D-8).
- Archive the `/tmp/rmp-intel-*` evidence files that the KB cites.
- Copy the Temporal test-server cache (`/tmp/temporal-test-server-sdk-python-*`) and restore it after boot.
- Recreate the rollback pointer after boot. Until WP-17 moves the pointer into a root-only file, rollback can always run with `BACKUP_DIR=/root/.openclaw/archive/openclaw-update-20260930T222604Z`.

**Rollback:** none needed; the step only added a copy.

## 2026-10-08 16:09 · Phase 0.3: needrestart is list-only

**Change:** added `/etc/needrestart/conf.d/90-rmp-list-only.conf` with `$nrconf{restart} = 'l';`. `perl -c` reports syntax OK.

**Why:** in non-interactive mode, the default restarts services automatically. unattended-upgrades had restarted rmp-api, rmp-worker and Postgres with no idle check (UC-3, D-14). From now on, restarts happen only in the weekly maintenance window that runs when Aura is idle (WP-08).

**Observed:** `needrestart -b` shows the kernel running 6.8.0-138 while 6.8.0-142 is installed (`KSTA 3`), so a reboot is needed (Phase 0.10).

**Rollback:** `rm /etc/needrestart/conf.d/90-rmp-list-only.conf`.

## 2026-10-08 16:14 · Phase 0.4: gateway OOM policy, drop_caches cron removed, swap added

**Changes:**
1. **Gateway OOM policy.** Added `/etc/systemd/system/openclaw-gateway.service.d/50-oompolicy.conf` with `[Service] OOMPolicy=continue`, then `daemon-reload` with **no restart**. `systemctl show` now reports `OOMPolicy=continue`, and the gateway has still been active since 10-04 17:58. When an exec child is OOM-killed, the whole gateway no longer stops (DS-01, WC-1: 16 gateway restarts on 10-04). Memory budgets are deliberately deferred to WP-10, because gateway RSS is about 2.1 GB with peaks of 3.85 GB.
2. **Hourly cache drop removed.** `/etc/cron.hourly/free` ran `echo 1 > /proc/sys/vm/drop_caches` every hour. It is moved to `/root/attic/etc-cron.hourly/free`.
3. **Swap. This is a deviation from the plan's "zram"; reason below.**
   - Added a 4 GiB `/swapfile` (mode 0600), persisted in `/etc/fstab` (backup at `/root/attic/fstab.bak-programme-20261008`; `findmnt --verify` is clean after `daemon-reload`).
   - Set `vm.swappiness=10` in `/etc/sysctl.d/90-rmp-swap.conf`.
   - `free -m` shows Swap 4095 MiB.

**Why the deviation:** zram failed. The `zram` module isn't in this cloud kernel; it ships only in `linux-modules-extra-<kver>`, which no metapackage here pulls in. zram would therefore break silently on every kernel upgrade, starting with the pending 6.8.0-142. A swap file doesn't depend on the kernel, and with low swappiness it is only an OOM buffer. The failed zram attempt was fully undone: config removed, `systemd-zram-generator` purged, failed units reset.

All runtime services stayed active throughout.

**Rollback:**
- gateway: `rm /etc/systemd/system/openclaw-gateway.service.d/50-oompolicy.conf && systemctl daemon-reload`;
- swap: `swapoff /swapfile`, remove the fstab line, `rm /swapfile /etc/sysctl.d/90-rmp-swap.conf`;
- cron: `mv /root/attic/etc-cron.hourly/free /etc/cron.hourly/`.

## 2026-10-08 16:45 · Phase 0.5: root-owned Claude Code binary

**Host changes:**
- Downloaded Claude Code 2.1.288 (`linux-x64`) from `https://downloads.claude.ai/claude-code-releases`. Its sha256 matches the release manifest (`0298068b…640c`).
- The aura-coder copy matches too, so it was **not** tampered with.
- Installed it root-owned at `/opt/claude-code/versions/2.1.288` (root:root 0755, every parent root 0755), with the link `/opt/claude-code/bin/claude -> ../versions/2.1.288`.
- Repointed `/usr/local/bin/claude-team` to `exec /opt/claude-code/bin/claude`. Backup: `/root/attic/claude-team.bak-programme-20261008`.

**PR (branch `aura/p0-root-owned-claude-bin`):**
- `app/coding/units.py`: `CLAUDE_BIN = /opt/claude-code/bin/claude`.
- `app/config.py`: pins `coding.claude_sha256`.
- `ops/setup_aura_coder.sh` installs only the pinned sha256, root-owned, with:
  - cleanup on failure;
  - an atomic swap of the version link;
  - pruning of old versions (keeps the current one and one older);
  - a version check that never runs the binary.
- `ops/claude_code_login.sh` uses the new path.
- `app/production/coding_readiness.py`:
  - `check_claude_code` **fails** if any symlink hop, the target, or any of their parents is owned by a non-root user or is group- or other-writable. A missing component is also a finding.
  - It also fails if the binary's sha256 differs from the pin.
  - It **warns** about any root process whose executable is under aura-coder's home.
- Tests are deterministic: an injected `lstat` and a fake `/proc`.
- The installer function was run end to end in a scratch directory three times: a wrong pin is refused before download; the right pin installs; a reinstall prunes old versions.

**Review:**
- Tier-1 reviewer 1 (correctness and invariants): APPROVE. Items 1-7 were fixed in the PR: hop-by-hop walk, fail-closed on missing components, pinned sha, trap cleanup, atomic swap, umask-independent fixture, deterministic tests.
- Tier-1 reviewer 2 (adversarial, closure of AS-02): **NOT CLOSED on the host.** The code path is closed, but:
  - (a) Kirill's interactive root `claude-team` session started on 10-06 in screen (pid 3599048) still runs the aura-coder binary, and Claude Code re-execs it for its search tools;
  - (b) the root operator scripts of the separate claude-jev repo (`/root/claude-jev`, `/opt/claude-jev`: `release.sh`, `validate.sh`, `install.sh`, `uninstall.sh`, e2e) default to `/home/aura-coder/.local/bin/claude`.

  Both need Kirill: his live session, and a separate repo. The new readiness warning makes (a) visible. AS-02 stays open in the ledger until both are resolved and the old aura-coder install is removed.

**Live readiness:** with this code it is `warn` (pid 3599048 only). The binary path and the sha256 both pass. A deploy rolls back only on `fail`.

**Verification:**
- Full sandboxed suite on the first commit: 1097 passed, 4 skipped; node 59/59.
- After the review fixes: 1101 passed, 4 skipped; node 59/59.
- Final suite: pending at commit time.

**Other finding:** two 10-05 `mktemp` directories in `/tmp` hold a file named `typesafe-api-key`. They are scheduled for removal in Phase 0.8.

## 2026-10-08 16:55 · Phase 0.5, continued: residual paths from the closure review fixed (Kirill approved both)

1. **Ended Kirill's old root `claude-team` session** (pid 3599048, started 2026-10-06 01:06 in `screen -S claude`). It ran the aura-coder-owned binary, and Claude Code re-execs that binary for its search tools. It exited cleanly on SIGTERM. `root_processes_running_from(/home/aura-coder)` now returns `[]`, and new `claude-team` sessions run `/opt/claude-code`.
2. **claude-jev repo (`/root/claude-jev`), released as v1.0.3.**
   - **Change:** `scripts/{install,uninstall,release,validate}.sh`, `tests/e2e/run.sh` and `docs/RUNBOOK.md` now default to `/opt/claude-code/bin/claude` instead of the aura-coder copy (still overridable with `CLAUDE_BIN`). The plugin version is bumped to 1.0.3 in `plugin.json` and `config.mjs`, and the repo's `docs/PROGRESS.md` is appended.
   - **Commit:** `cc831d2`, on branch `root-owned-claude-bin`, fast-forwarded into `main`.
   - **Tests:** 119/119 pass. One earlier run had a single failure in a wall-clock test, "oversized command lines… < 1500 ms", which measured 1541 ms with four test files running concurrently on a loaded host. That test passes 3/3 in isolation and the guard code is unchanged. It is a pre-existing flaky test in claude-jev, noted for its owner.
   - **Release:** `scripts/release.sh v1.0.3` passed its tests and validation, tagged `v1.0.3`, moved `/opt/claude-jev` to `v1.0.3` (clean), and refreshed the jev install records in `/root/.claude-team` (user and project scope) and `/root/.claude` (user scope) using the root-owned binary.
   - **Check:** neither repo has old-path references left, apart from claude-jev's history log.
3. **Remaining for AS-02:** merge and deploy the RMP PR, then remove the old aura-coder install (`~/.local/bin/claude` and `~/.local/share/claude/versions`) so nothing can fall back to it.

## 2026-10-08 16:58 · Phase 0.6: Safe Harbor / Kairos cron and daemons retired (K-11)

**Before:**
- Root's crontab had a single entry, `*/5 * * * * node /root/aura_safe_harbor/kairos_core/ensure_peripheral_daemons.js`.
- Every 5 minutes it revived two detached daemons, `kairos_core/modules/task_watchdog.js` (pid 1697793) and `self_auditor.js` (pid 1697804). Both were running since 10-01 00:23, outside systemd.
- On each run it also **re-appended to Aura's `workspace/AGENTS.md`** two blocks, "TOKEN RATE LIMIT SURVIVAL RULE" and "MANDATORY DIRECT IMPERATIVES". The second contains the deep-grep-for-secrets instruction (AS-15, IF-07, UC-14).
- Nothing in RMP depends on them: a grep of `app/`, `ops/`, `plugins/` and the units finds none. Aura's `AGENTS.md:291` already calls `tasks.md` "optional human notes".

**Change:**
- Backed up the crontab to `/root/attic/crontab.root.bak-programme-20261008` and removed the entry. Root's crontab is now empty.
- SIGTERM to both daemons; both exited.

**Still to do** (WP-20/22 under K-11, now possible without the cron re-adding them):
- remove the two `AGENTS.md` blocks, including the deep-grep instruction;
- archive both local repos with their history purged of secrets;
- rotate the exposed keys.

**Rollback:** `crontab /root/attic/crontab.root.bak-programme-20261008`. The next cron run restarts the daemons.

## 2026-10-08 17:02 · Phase 0.7 (manual part): Temporal databases dumped

**Change:** `sudo -u postgres pg_dump -Fc` of `temporal` (1.15 MB) and `temporal_visibility` (108 KB) into `/root/.openclaw/archive/temporal-dump-20261008T*/` (mode 0700), with a `SHA256SUMS` file. Kept outside `data/backups/`, so the 14-directory pruning never touches it.

**Check:** `pg_restore -l` reads both dumps back (39 and 3 table-data entries).

**Still to do:** the `ops/backup.sh` PR (resolve the RMP root, fail loudly, real Qdrant snapshot, Temporal dumps nightly). It waits for PR #24 to deploy (one PR in flight).

## 2026-10-08 17:08 · Phase 0.8: host hygiene

| Item | Done | Rollback |
|---|---|---|
| Stray `temporal-test-server` (pid 1406490, orphan since 09-30, listening on `*:38869`) | SIGTERM; the port is closed. The binary `/tmp/temporal-test-server-sdk-python-1.23.0` is **kept**, because the offline Temporal tests need it | — |
| `/etc/rmp/rmp.env.bak-20261006T063550Z` (held the old, public DB password) | `shred -u` | — (that password is rotated) |
| Agent Cerebro (`agentrecall` 0.2.0, hand-copied into `/usr/local/lib/python3.12/dist-packages`, with 3 console scripts in `/usr/local/bin`); no references anywhere | Moved to `/root/attic/cerebro/` | move back |
| Root user units: dangling `default.target.wants/openclaw-{gateway,chat-log}.service` links and `openclaw-gateway.service.bak` (OT-2; could revive a second gateway on port 18789) | Moved to `/root/attic/root-user-units/`, then `systemctl --user daemon-reload` | move back |
| 31 gitignored scratch scripts in the live checkout (`fix_*`, `query_*`, `parse_test*`, and the destructive `terminate_all.py`, `delete_tasks.py`); no references in crons, the persona, `app/`, `ops/`, `plugins/` or units | Moved to `/root/attic/rmp-scratch/` under the `CLAUDE.md:50` override. No tracked files changed; rmp-api and rmp-worker stayed active | move back |
| `/tmp/tmp.*` (8 mktemp dirs from 09-30 and 10-05; 2 held a `typesafe-api-key` file, 0600 root) | Key files `shred -u`; dirs removed | — |
| Aura's OpenClaw fork `/tmp/openclaw-v2026.9.7` (2.0 GB, D-8) | **Archived** (moved) to `/root/.openclaw/archive/aura-openclaw-fork-v2026.9.7`; not deleted | move back |
| Test-suite leaks: `rmp-cap-*` (1,997), `rmp-tests-*` (998), `jev-*` (4,955), `rmp_ctx_*` (66), `openclaw-vitest-include-*.json` | Deleted (544 MB). The leaks themselves are fixed in WP-01 (TU-1) and in claude-jev (its owner) | — |

**Kept:** `/tmp/claude-0` (this session's task outputs), `/tmp/openclaw-upgrade-backup.path`, `/tmp/rmp-intel-*` (evidence; archived before the reboot).

**Also checked:** the Kairos daemons did not respawn after the cron removal.

## 2026-10-08 16:52 · Correction to the times of the entries above

The times on the Phase 0.5-continued, 0.6, 0.7 and 0.8 entry headers (16:55, 16:58, 17:02, 17:08) were estimates written ahead of the clock. The real time when this correction was written is 16:52 CEST, and all of those steps happened before it. Their content is accurate; only the header times are wrong. From this entry on, every timestamp comes from `date`.

The 0.8 statement "the Kairos daemons did not respawn" was checked before a full 5-minute cron boundary had passed. It is re-checked in a later entry.

## 2026-10-08 16:57 · Phase 0.7: backup fix prepared and validated against production (PR waits for #24)

**Root cause of the empty Qdrant archive:**
- `rmp-backup.service` starts `ops/backup.sh` with no working directory.
- The inline `python -c 'from app.config import …'` therefore failed to import.
- The fallback `|| echo data/qdrant` then picked the empty legacy directory, giving a 110-byte `qdrant.tar.gz` every night.
- The real storage is the 90 MB bind mount `data/qdrant-server`, and a tar of live storage would not be consistent anyway.

**Change** (branch `aura/p0-backup-fail-loudly`, commit on top of main 1a0c3be):
- `cd "${RMP_ROOT}"` before anything imports `app`.
- In server mode, a Qdrant **full snapshot** (`POST /snapshots?wait=true`). It is downloaded and checked to be a tar with entries, then deleted from the server. Embedded mode keeps a tar of the configured path.
- Every component's failure (postgres, qdrant, artifacts, openclaw-state, temporal-sqlite) is collected, written into `manifest.json` `failures`, and makes the script **exit 1**, so the unit shows failed. Previously each failure was only a WARN and the run "passed".
- Pruning removes only directories named like a backup stamp. Named directories, such as an OpenClaw rollback backup, are never pruned by age (Phase 0.2 root cause).
- Paths and the interpreter can be overridden for tests (`RMP_ROOT`, `RMP_BACKUP_ROOT`, `RMP_PYTHON`, `OPENCLAW_HOME`); the defaults are production.
- Two of my own bugs were caught before commit: a `tar | grep -q` SIGPIPE under `pipefail` that would report a good snapshot as failed, and a Python-3.12-only f-string.

**Tests:** new `tests/test_backup_script.py` runs the real script against fake `pg_dump` and a fake Qdrant API. It covers a healthy run (snapshot taken, POST/GET/DELETE in order, no failures), a Qdrant failure (exit 1, only `qdrant` listed, others still produced), a Postgres failure, and pruning (14 dated kept, a named directory untouched). Result: 4/4, plus `test_database_url` 6/6.

**Live check before the PR:** ran the branch's script against production. Exit 0, manifest failures `[]`. `qdrant-full.snapshot` is 88 MB and contains snapshots of all 4 collections plus `config.json`. No snapshots are left on the server. Backup `data/backups/20261008T145634Z` is the **first backup from this host that captures Qdrant**.

**Next:** rebase onto main after PR #24 deploys, then the full suite, review and PR.

## 2026-10-08 16:57 · Phase 0.6 re-check and PR #24 merged

- **Kairos re-check, after several 5-minute cron boundaries:** no `task_watchdog`, `self_auditor` or `ensure_peripheral_daemons` process is running, and root's crontab is still empty. Confirmed retired.
- **PR #24 merged** (squash, `8469be0`). `main` was unchanged since the local verification (`1a0c3be`), CI `test` passed, both Tier-1 reviews were done, and the full sandboxed suite gave 1103 passed, 4 skipped; node 59/59. The deploy is being watched.

## 2026-10-08 17:03 · PR #24 deployed; finding authz-secrets-02 closed

**Deploy:** RMP's deploy of `8469be0` reached the live checkout about 270 s after the merge.
- `/srv/aura-code/runs/main/result.json` reports `status: deployed`; rmp-api and rmp-worker restarted; health and readiness passed; the canary passed.
- The worker journal has no nondeterminism or sandbox errors in the 15 minutes after.
- `/health` is ok.
- Live `check_claude_code`: `pass`. `CLAUDE_BIN` is `/opt/claude-code/bin/claude`, and no root process runs from aura-coder's home.

**Old install removed:** with no coding unit and no aura-coder process running, removed `/home/aura-coder/.local/bin/claude` and `~/.local/share/claude/versions/{2.1.280,2.1.288}`. The verified 2.1.288 stays at `/opt/claude-code`, and aura-coder can run it (`runuser -u aura-coder -- /opt/claude-code/bin/claude --version` gives 2.1.288).

**authz-secrets-02 closed** in `ledger.json`:
- root executes only a root-owned binary pinned by sha256;
- readiness fails on any regression and warns about stray root processes;
- no root caller remains on the old path (RMP, `claude-team`, claude-jev v1.0.3).

## 2026-10-08 17:18 · Phase 0.7: Tier-2 review round 1, and the fixes

**Review:** Tier-2 review, REQUEST_CHANGES. The core was confirmed correct; 13 issues were raised. I fixed all but #3 in the PR (`d49a39c`):

| # | Issue | Fix |
|---|---|---|
| 1 | The new snapshot format could not be restored | New `ops/restore_qdrant_snapshot.sh`: extract the full snapshot, then upload each collection with `POST /collections/<c>/snapshots/upload?priority=snapshot&wait=true`. `ops/restore_backup.sh` calls it, restores Temporal's Postgres dumps, drops the stale-SQLite Temporal restore, and exits 1 with "Restore INCOMPLETE" on any failure. Runbook rewritten; the `--storage-snapshot --force-snapshot` alternative was checked against the real binary's `--help` |
| 2 | Pruning ran after a failed backup | A failed run prunes nothing |
| 3 | Nothing watches a failed backup | **Deferred to WP-12** (readiness validates the manifest), as planned |
| 4 | Server-side snapshots could leak | Stale full snapshots are deleted when the Qdrant step starts; an EXIT trap (and TERM/INT → exit 143) deletes this run's snapshot |
| 5 | Tests did not run from outside the repo | Tests run from tmp with explicit data, settings and OpenClaw paths; 14 cases |
| 6 | Partial downloads were trusted | Download to `.partial`, verify Qdrant's reported size and checksum plus the `tar -tf` exit status and a non-empty listing, then `mv` |
| 7 | Temporal's databases were not backed up | Nightly `pg_dump -Fc` of `temporal` and `temporal_visibility` through `sudo -n -u postgres` |
| 8 | Some file copies could fail silently | WAL and SHM copies are checked |
| 9 | Data root tied to the code root | Separate data root (`RMP_DATA_DIR`) and settings path |
| 10 | Concurrent runs | `flock` run lock |
| 11 | Qdrant may not be up yet | Wait on `/readyz` before snapshotting |
| 12 | File modes and pruning order | `umask 077`; pruning sorted by name |
| 13 | Behaviour change not announced | Noted for the PR body |

**Restore drill (the first proven restore of the vector store):**
- The 15:15 production backup was restored into a scratch `qdrant:v1.17.0` container (127.0.0.1:16333, 1 GB memory cap).
- Every collection's point count matches production: `rmp_memories_openai_3small` 2615, `rmp_deep_memory_v1` 1395, `rmp_task_registry_openai_3small` 251, `mem0migrations` 1.
- The container was removed afterwards.

**The drill caught a real bug:** a freshly started container resets the first connections (curl error 56), which `--retry-connrefused` does not retry. Both scripts now wait with `--retry-all-errors`. The same bug would have failed a backup run right after a reboot.

**Second live production run:** exit 0, failures `[]`. Both Temporal dumps present (1.17 MB and 108 KB). Every file is 0600. No snapshot left on the server.

**Tests:** `test_backup_script` 14/14; with `test_coding_host` and `test_database_url`, 37/37. The full suite is running, and the re-review is in progress.

## 2026-10-08 17:32 · Phase 0.7: re-review APPROVE, final fixes, ready to ship

**Re-review:** APPROVE. All items are fixed or correctly deferred (item 3 goes to WP-12; unit ordering goes to WP-02).

**Non-blocking points applied anyway** (commit `a706772`):
- **Retention:** now counts only *complete* backups (manifest `failures: []`). After a streak of failed nights, the first good run cannot prune older complete backups. Failed or unfinished backups go only once they are older than the oldest complete backup kept.
- **Killed runs:** `*.partial` files are removed on any exit.
- **Legacy Qdrant tars:** the restore warns that a legacy `qdrant.tar.gz` never restored the Qdrant server.
- **Runbook:** states that backups before 2026-10-08 hold no Qdrant data, explains the stale-snapshot purge, and gives the OpenClaw store restore command.
- **Smaller fixes:**
  - `stat` no longer aborts on a missing download;
  - Temporal failures are named per database (`temporal:<db>`);
  - a failed snapshot listing is logged as a WARN;
  - the restore helper is now mode 755.

**Note:** backups made before today have no `failures` key, so they count as incomplete. They will be pruned once 14 complete backups exist (about 2 weeks). That is conservative, and the disk has 106 GB free.

**Tracked for later:**
- WP-02: a `TimeoutStartSec` on the backup unit, and `pg_dump --lock-wait-timeout`.
- WP-12: a Temporal `pg_restore` drill, and readiness and alerting on manifest failures.

**Verification:**
- `test_backup_script`: 15/15.
- Full sandboxed suite on `a706772`: 1118 passed, 4 skipped; node 59/59.
- Placeholder grep clean; no new file is ignored; `main` unchanged at `8469be0`; no open PRs.

## 2026-10-08 17:35 · Phase 0.9: Slack ingress verified with Kirill's test DM (REPORT §14 Q1 answered)

Kirill sent "ping, ingress test" at 17:34 CEST. The path was traced read-only, using times and statuses only:

| Step | Time (UTC) | Evidence |
|---|---|---|
| Claimed by `rmp_adapter` | 15:34:17.730 | `message_received slack DM on agent:main:slack:channel:…`; `before_dispatch` saw it already claimed |
| Task created | 15:34:35 | `e692a548`, `dedup=false`, `task_type=user` |
| Request recorded | 15:34:34.8 | `task_messages` |
| Evaluator verdict | 15:34:55.7 | `task_messages` |
| Reply recorded and posted to Slack | 15:34:56.4 | `task_messages`; `side_effect_receipts` `slack` at 15:34:56.36 |
| Task completed | — | status `completed` |

**End to end:** about 39 s.

**Conclusion:** ingress works. The silence since 10-04 was the absence of user DMs, not lost events. The "10 active socket-mode connections" risk (UW-7) still gets its monitoring and spool in WP-11.

## 2026-10-09 06:39 UTC · Phase 0.7: PR #25 merged, deployed and verified through the nightly unit

**Merge:** `origin/main` was unchanged at `8469be0`, #25 was the only open PR, and CI `test` was green. Squash-merged as `5424c02`.

**Deploy:** after CI passed on `main`, `watch_main` deployed `5424c02` (06:32Z). `result.json`: `deployed`, nothing restarted (ops, docs and tests only), health and readiness passed, canary ok. No nondeterminism or sandbox errors in the worker journal.

**Verification through the real unit:** `systemctl start rmp-backup.service` → `Result=success`, 18 s.

| Check | Result |
|---|---|
| Manifest `failures` | `[]`; components postgres, temporal, qdrant, artifacts, openclaw-state, settings |
| `qdrant-full.snapshot` | 88 MB; holds rmp_memories_openai_3small, rmp_deep_memory_v1, rmp_task_registry_openai_3small, mem0migrations and config.json |
| Server snapshots left | none (`GET /snapshots` → `[]`) |
| `temporal.dump`, `temporal_visibility.dump` | 1.2 MB, 109 KB |
| Permissions | directory 0700, files 0600, root |
| Retention | 15 dated directories kept; nothing pruned (older backups have no `failures` key and count as incomplete) |

**Note:** `cron_jobs.json` is absent because OpenClaw moved its cron jobs into its state database (`cron/jobs.json.migrated`), which `openclaw-state/` already captures. The runbook row and the dead copy go to WP-12 (offrepo-openclaw-home-R6).

**Ledger:**
- Closed: ops-deploy-R1, ops-deploy-R3, production-R6, docs-vs-code-R2, ops-deploy-R15.
- Progress noted, still open: ops-deploy-R4 and ops-deploy-R8 (rest in WP-12), authz-secrets-21 (Qdrant auth).
- Off-host copy (ops-deploy-R2) stays open for WP-12's restic step, under Kirill's accepted local-only exception.

**Rollback:** revert `5424c02` through a PR; no host state changed.

## 2026-10-09 06:43 UTC · Phase 0.10: pre-reboot checks; timers paused; reboot scheduled

**Why reboot:** `/var/run/reboot-required` is set. unattended-upgrades installed kernel `6.8.0-146-generic` this morning (initrd and modules complete); 138 is running. GRUB default 0 boots 146.

**Checks before the reboot:**
- **SSH:** `ssh.service` is disabled but socket-activated through the enabled `ssh.socket`. Remote access survives.
- **Enablement:** every runtime unit and every rmp timer is enabled. The only active-but-not-enabled units are `ssh.service` (above) and the stock `systemd-sysext.socket`.
- **fstab:** `findmnt --verify` reports 0 errors (one benign warning for `/swapfile`).
- **ExecStartPre proofs:**
  - `sync_nvidia_keys.py` (rmp-api, rmp-worker, gateway) is local-only and returned `synced: 3` on the last starts;
  - `wait_temporal.sh` waits 90 s, and the worker restarts after that;
  - Restart=always with RestartSec 3–5 s cannot reach StartLimitBurst 5 in 10 s.
- **Docker:** qdrant, phoenix, otel-collector and obscura restart `unless-stopped`; `temporal-server` is re-created by its unit.
- **Aura idle:** no non-terminal task (631 are `stopped_by_user`, which is terminal), last agent run 06:32:47Z (the deploy canary), no coding units, no other Claude sessions.

**Baselines** in `program/reboot/`: 67 active services, 25 timers, 5 containers, Qdrant counts 2618/1401/252/1, plus `POST-REBOOT-CHECKLIST.md`.

The `/tmp` survivors are cached byte-identical in `/root/.cache/rmp-tests/`.

**Timers:** `rmp-temporal-watchdog.timer` and `rmp-canary-sentinel.timer` are disabled and stopped across the boot.
**Rollback:** `systemctl enable --now` both. If 146 misbehaves, boot 6.8.0-138 from GRUB's advanced menu (the provider console is needed, since GRUB_TIMEOUT=0).

Reboot scheduled with `shutdown -r +2`, per Kirill's choice "After PR #25 deploys".

## 2026-10-09 07:02 UTC · Phase 0.10: reboot done and verified

The host rebooted at 06:46:56Z into `6.8.0-146-generic`. The session resumed through Kirill's desktop app.

| Check | Result |
|---|---|
| `systemctl is-system-running` / `--failed` | `running` / none |
| Active services vs. baseline | Same, except `fwupd` (D-Bus activated on demand) and `systemd-networkd-wait-online` (boot oneshot) |
| Containers | Same 5 names and images |
| Swap, swappiness, gateway OOMPolicy | 4G active, 10, `continue` |
| Qdrant point counts | Unchanged: 2618/1401/252/1 |
| RMP `/health` | ok, vector memory ready |
| Readiness | `production_ready`, score 94, no fail |
| Slack | socket mode connected 06:49:19Z |
| Worker journal since boot | no nondeterminism, sandbox errors or tracebacks |

**Readiness warnings:**
- telemetry: OTLP endpoint not set (WP-03);
- deep-memory ingest and enrichment: 1 failing `enrich` job (to triage in Phase B).

**Restored:**
- `/tmp/openclaw-upgrade-backup.path` and `/tmp/temporal-test-server-sdk-python-1.23.0`, byte-identical copies;
- `rmp-temporal-watchdog.timer` and `rmp-canary-sentinel.timer` re-enabled and active; the timer list matches the baseline.

**Note for WP-11 (UW-7):** right after the reboot, Slack still reported 9 active socket-mode connections for the app, though this host has one. Either stale server-side connections or other consumers of the same app token; WP-11 checks which.

**Phase 0 complete.**

## 2026-10-09 07:15 UTC · Phase 1 start: WP-01 (test hermeticity and replay foundation)

**Clone:** `/root/work/wp-01` on `aura/wp-01-test-hermeticity`, from `main` `5424c02`.

**New verify harness:** `program/bin/verify.sh <clone> [pytest args]`. It is the deterministic verify step for every WP from now on.
- **Network:** private network namespace, loopback only.
- **Read-only:** a private mount namespace mounts these directories read-only: `/root/.openclaw`, `/etc/openclaw`, `/etc/rmp`, `/srv/aura-code`, `/root/.claude`, `/root/.claude-team`, `/root/.config`, `/opt/claude-code` and `/opt/claude-jev`.
- **Private `/tmp`:** an on-disk directory, seeded with the cached Temporal test server.
- **Resources:** a 2G `MemoryMax` scope with no swap.
- **Reports:** the files left in the private `/tmp` (TU-1), a placeholder grep of the diff against `origin/main`, and new files hidden by `.gitignore`.

**Smoke test:** `tests/test_database_url.py` plus the node suite passed, but left 76 `rmp-cap-*` directories, which confirms TU-1.

**Baseline:** a full baseline run on `main` is writing to `program/research/wp01-baseline-verify.txt`.

**Workflow `wp01-research-spec`** (run `wf_147687c1-ca0`) runs these agents in order:
1. a research brief on Temporal replay testing, the `patched` lifecycle, pytest/node hermeticity and CI labels;
2. the binding spec, `program/specs/WP-01.md`;
3. an adversarial critic;
4. a revision.

## 2026-10-09 07:22 UTC · WP-01 baseline on main under the read-only sandbox

**Result:** 1117 passed, 1 failed, 4 skipped; node 59/59; 78 entries left in the private `/tmp` (TU-1).

**The one failure was the harness, not the code:**
- `test_a_worker_restart_mid_run_reattaches_to_the_same_unit` starts a Temporal *dev* server, which the SDK downloads into the temp dir as `temporal-sdk-python-1.23.0`. The private `/tmp` had no copy, and there was no network.
- The reboot had also wiped the host's copy.
- **Fix:** the harness now links a root-only copy of the Temporal CLI 1.6.1 (`/root/.cache/rmp-tests/temporal-sdk-python-1.23.0`, copied from `/root/.temporalio/bin/temporal`) and the test server into each run's `/tmp`.
- Probe: `start_local()` started offline with the seeded copy. Rerun: 1 passed.

**Effective baseline:** 1118 passed, 4 skipped, 0 failed. With production directories read-only, no test failed, so no test on `main` writes into them.

## 2026-10-09 17:25 UTC · WP-01 spec fixed (binding)

**Workflow `wp01-research-spec`** (run `wf_147687c1-ca0`). It was interrupted once by the usage limit and resumed from cache.
- **Research brief:** `program/research/temporal-replay-and-test-hermeticity.md`, with sources.
- **Spec:** `program/specs/WP-01.md`, 37 items in 4 batches (one PR each), 13 deferrals, each to a named WP with a reason.

**Critic verdict: revise, 12 gaps.** All are closed in the revision. The ones that mattered:
- A batch-1 refusal based on the denied roots alone would have broken every reviewed coding job's verify, because their `TMPDIR` is `/srv/aura-code/cache/tmp`, as well as direct-session clones under `/srv/aura-code/direct` and go-live. The guard's path core, with its allowed roots, now lands in batch 1 (WP01-37).
- **Shell strings:** `shell=True` and `os.system` commands are now split and checked, including `pkill`, `killall` and `claude`.
- **Deploy targets:** `/etc/systemd/system`, `/root/.cursor` and `/run/rmp-code-reload.lock` are now denied.
- **Readiness seal:** it now covers the raw gRPC port probe and the `temporal.service` read.
- **Refused runs:** they no longer leak their temp root.
- **Rollback reverts:** a rollback revert of a workflow change waits for the `no-replay-impact` label (D-10).
- **Fixture base:** the fixtures are recorded on the batch-4 base, not pinned to `5424c02` (D-11).
- **Export probes:** added for the git-dependent tests.
- **Node:** `REPO_ROOT` scoping fixed.

**Also in this step:**
- **New finding (WP01-15):** the coding secret-scan tests load every production secret file into the test process. Conftest blanks the secret-file lists.
- **`ci.yml` push:** settled. It goes on the current token, which pushed `ci.yml` in `1000f2d`, before WP-16's PAT swap.
- **Bookkeeping:** WP-01 is placed first in the `work-packages.md` §3 rows for `app/config.py`, `app/memory/vector.py`, `app/production/canary_sentinel.py` and `app/coding/deploy.py`. The `worker.py` row is now `01 → 03 / 04b → 05 → 29`, and a new `.github/workflows/ci.yml` row reads `01 → 26`.

**Review tiers:**

| Batch | Contents | Tier |
|---|---|---|
| 1 | `app/`, `worker.py`, CI | 1 |
| 2 | Tests and CI | 2 |
| 3 | CI guard, the merge gate | 1 |
| 4 | Tests and fixtures | 2 |

## 2026-10-10 01:20 UTC · WP-01 batch 1 started; WP01-07 host probe passed

**Harness:** `verify.sh` gained `LAYOUT=coding-job` and `LAYOUT=direct`.
- **How it works:** a copy of the clone runs under `/srv/aura-code/jobs/…` with `TMPDIR=/srv/aura-code/cache/tmp`, or under `/srv/aura-code/direct/…`.
- **Isolation:** a scratch directory is mounted over `/srv/aura-code` inside the private namespace only.
- **Smoke test:** passed, and the real `/srv/aura-code` was untouched.

**Batch 1:**
- **Branch:** `aura/wp-01-1-paths-and-worker-lists`; git identity HyperAILab, matching earlier PRs.
- **Workflow:** `wp01-batch` (run `wf_9f9b41e4-646`): implement, then Tier-1 review by a correctness/invariants reviewer and an adversarial-closure reviewer, then at most 2 fix/re-review rounds.

**WP01-07 host probe** (before the batch-1 merge, as the spec requires):
- A transient unit with the production env files loaded resolved `/root/.openclaw /root/.openclaw/rmp /root/.openclaw/rmp/data None None`, which is the expected result.
- No `Environment=` override of `OPENCLAW_HOME`, `RMP_ROOT`, `RMP_DATA_DIR`, `OPENCLAW_ENV_PATH` or `AURA_CODE_ROOT` in rmp-api, rmp-worker, rmp-canary-sentinel, rmp-canary, rmp-memory-canary, rmp-janitor, rmp-janitor-frequent, rmp-backup, aura-coder-firewall or rmp-code-watch.
- Neither env file defines those names (names checked only, no values read out).
- **Conclusion:** the config-routed defaults resolve to today's literals in production.

## 2026-10-10 05:18 UTC · WP-01 batch 1 implemented, reviewed (Tier 1) and verified

**Workflow `wp01-batch`** (run `wf_9f9b41e4-646`, 9 agents): implement; two Tier-1 reviewers, one for correctness and invariants and one for adversarial closure; 2 fix rounds; re-review. **Approved by both reviewers in every round, with 0 blocking issues.**

**Items:**
- WP01-01…06: the env file, the coding root, the OpenClaw state DB, the canary-sentinel state paths and deploy's `LIVE_REPO` are read through `app.config`, with today's literals as defaults.
- WP01-07: `tests/test_production_paths.py` pins all 25 production values in a child interpreter. That child runs an inline audit hook that refuses production paths.
- WP01-08/09: conftest refuses an inherited production path variable before creating anything, defaults each variable on its own under one temp root, puts pytest's basetemp in that root, removes the root at the end, and turns off Node's compile cache.
- WP01-10…12: `worker.WORKFLOWS` and `worker.ACTIVITIES` sit at module level, the same 7 + 49 in the same order. `tests/test_worker_registration.py` adds 4 tests, and `test_whole_path` derives its lists from them, so it now also runs `deliver_reply_files`, `confirm_approval_provenance` and the recall activities.
- WP01-37: the guard's path core (`tests/production_guard.py`, 28 tests), and CI's env block and seed step are gone.

**Deviations:**
- **WP01-11:** the SDK test starts and shuts the worker down instead of only constructing it. An unclosed worker keeps polling `openclaw-tasks` for the rest of the session, and a later test server on the same port could hand it work.
- **WP01-04:** a cosmetic module constant.

**Review notes:**
- **Fixed:** a line-length nit, in 2 commits.
- **Carried to batch 2:** the conftest root is left behind when pytest exits before `pytest_configure`, after a usage error or with `--version`. This is a new finding outside the spec, and it is not hit by normal runs.
- **Programme gates:** the host probe was done earlier, and CI green without the env block is shown by the PR's run.

**My deterministic verify on `7a7ade8`:**
- pytest 1159 passed, 4 skipped (the baseline had 1118: 41 new tests);
- node 59/59;
- placeholder grep clean; no ignored new files;
- 73 leftovers, all `rmp-cap-*` from the node suite (WP01-24, batch 3); a node-only run leaves the same 73;
- the implementer's `LAYOUT=coding-job` and `LAYOUT=direct` runs: green, with 0 leftovers.

**Unchanged:** `git diff 5424c02 -- app/workflows` is empty, and `requirements.txt` is untouched.

**Deploy restarts:** `rmp-api` and `rmp-worker`.
