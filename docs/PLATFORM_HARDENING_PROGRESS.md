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
