# Backup & Restore Runbook

## Daily backup

Runs automatically from `rmp-backup.timer` at 03:15 UTC.

Manual run: `bash /root/.openclaw/rmp/ops/backup.sh`. Only one backup runs at a time; a second run exits 1 while the first holds `data/backups/.backup.lock`.

Backups are stored in `/root/.openclaw/rmp/data/backups/<YYYYMMDDTHHMMSSZ>/`. Files are mode 0600, and the directory sits under the 0700 `/root/.openclaw`.

| File | Contents |
|---|---|
| `rmp_db.dump` | RMP's Postgres ledger (`pg_dump -Fc`) |
| `temporal.dump`, `temporal_visibility.dump` | Temporal's own Postgres databases: open workflows and their histories |
| `qdrant-full.snapshot` | A full Qdrant snapshot taken through its API. It holds one snapshot per collection plus `config.json`. Its size and checksum are verified against Qdrant's report, and the copy on the server is deleted afterwards |
| `qdrant.tar.gz` | Embedded-mode Qdrant only. **Backups made before 2026-10-08 hold no Qdrant data**: their `qdrant.tar.gz` is a 110-byte tar of an empty directory |
| `artifacts.tar.gz` | The evidence store |
| `openclaw-state/` | OpenClaw's agent and state SQLite stores, taken with the backup API and checksummed |
| `temporal.db` | The legacy Temporal SQLite file from before Temporal moved to Postgres. It is no longer live |
| `settings.json`, `openclaw.json`, `cron_jobs.json` | Config snapshots |
| `manifest.json` | Contains `"failures"`, which is empty when every component succeeded |
| `backup.log` | The run's log |

**When a component fails:**
- Every other component still runs.
- The failure is listed in `manifest.json` and in the log.
- The script exits 1, so `rmp-backup.service` shows as failed.
- **No older backup is pruned.**

Check the last run with `systemctl status rmp-backup.service`, then read the newest manifest:

```bash
jq . "$(ls -1d /root/.openclaw/rmp/data/backups/2*/ | sort | tail -1)manifest.json"
```

**Retention:**
- The newest 14 *complete* backups are kept. A complete backup has a manifest with no failures.
- Failed or unfinished backups never count toward the 14. They are pruned only once they are older than the oldest complete backup kept.
- Directories that are not named like a backup stamp are never pruned by age; an OpenClaw rollback backup is one example.

**Qdrant snapshots:** the backup deletes every *full* snapshot it finds on the Qdrant server, because only this script makes them. Keep a hand-made full snapshot elsewhere.

## Restore

A restore takes Aura offline: it needs Kirill's OK and an idle window.

```bash
bash /root/.openclaw/rmp/ops/restore_backup.sh /root/.openclaw/rmp/data/backups/YYYYMMDDTHHMMSSZ
```

**On a new host**, the Postgres roles `rmp` and `temporal` must exist before the restore, because each archive names its owner. No backup holds them or their passwords. Create each as the postgres superuser with the password from `/etc/rmp/rmp.env` (`DATABASE_URL`) or `/etc/openclaw/openclaw.env`; the prompt keeps it off the command line:

```bash
sudo -u postgres createuser --pwprompt rmp
sudo -u postgres createuser --pwprompt temporal
```

The script does the following:
1. Asks for confirmation.
2. Takes the code-reload lock (`/run/rmp-code-reload.lock`) and holds it until it exits: a deploy waits for it, a code reload skips. It refuses before anything stops while the lock is held or an `aura-deploy-*` unit is active. The watchdog and the canary sentinel do not take the lock; step 3 stops their timers.
3. Stops every writer: each active `rmp-*` timer, then the service it triggers (a running canary, janitor, watchdog or backup run ends), then `rmp-code-watch` and `openclaw-gateway` when active, then `rmp-api`, `rmp-worker` and `temporal`. `rmp-qdrant` keeps running for step 6. Then it checks for a deploy unit again, because rmp-api or rmp-worker may have launched one before they stopped. A failed stop, a deploy unit or a signal ends the restore here: it starts again what it stopped, exits non-zero, and nothing was restored.
4. Restores `rmp_db` as an exact copy, through the postgres superuser (`ops/restore_pg_database.sh`). `pg_restore --clean --if-exists --create --exit-on-error -d postgres` drops the database and recreates it from its archive: its owner, encoding and settings, the `vector` extension, and nothing the dump lacks. The `rmp` role cannot do this: it has neither CREATEDB nor superuser. The first error stops it, so a `DROP DATABASE` refused because a session is still connected changes nothing.
5. Restores both Temporal databases the same way.
6. Restores Qdrant collection by collection into the **running** Qdrant (`ops/restore_qdrant_snapshot.sh`).
7. Restores artifacts, OpenClaw's stores and settings. The stores (sessions, transcripts, auth profiles, cron jobs) are coupled to `rmp_db`, so they come back with it. `ops/backup_openclaw_state.py restore` checks every copy's checksum and integrity before it replaces anything, and refuses while the gateway runs.
8. When every part was restored, starts what it stopped: `temporal`, `rmp-api`, `rmp-worker`, then the gateway and `rmp-code-watch`, then the timers. Then it runs the timer check, one `next: <timer>` line per timer. A timer whose interval passed during the restore runs its service at once and shows no next elapse until that run ends, so the restore waits up to 15 minutes for such a run. A `NO NEXT: <timer>` line left makes it exit 1. Then wait for the run to end, or re-arm the timer: `systemctl start <timer>`, and if it still has no next elapse, `systemctl start <stem>.service` once, in an idle window. The check is `systemctl list-timers 'rmp-*' --all --no-pager --no-legend | awk '{print ($1 == "-" ? "NO NEXT: " : "next: ") $(NF-1)}'`.

**Not restored by the script:** `openclaw.json`. A verbatim copy undoes what RMP applied to it since the backup, so put it back by hand only when the live file is lost or damaged, then apply RMP's model policy to it again:

```bash
cp -a <backup>/openclaw.json /root/.openclaw/openclaw.json
cd /root/.openclaw/rmp && venv/bin/python -c 'from app.llm.model_policy import apply_openclaw_policy; print(apply_openclaw_policy()["changed"])'
```

**If a part fails** after the first database step, or a signal ends the restore before its last step (Ctrl-C, a `kill`, or an SSH session that hangs up), the restore starts nothing, because a store may be half restored: rmp-api runs `create_all` on `rmp_db` at start, and the watchdog would restart Temporal on a partial database. It exits non-zero and prints:
- `Restore INCOMPLETE: not restored: …` for a failed part, and `Restore INCOMPLETE: it ended before its last step` when a signal ended it;
- the units it left stopped, and the one `systemctl start` command that starts them in order;
- a warning not to start rmp-api, rmp-worker or openclaw-gateway, run a deploy or reboot before the stores are whole.

Make the stores whole, by restoring the failed part again or running the whole restore again, then run the printed `systemctl start` command. A second run of the restore starts only what it stopped itself, so the first run's command is still needed.

**Residual risk:** after a partial failure, only a person can start the services on a half-restored store. That happens through a `systemctl start`, a deploy unit started by hand, or a reboot, because all four units are enabled. The restore's last line warns against all three.

**Qdrant only:**

```bash
bash /root/.openclaw/rmp/ops/restore_qdrant_snapshot.sh <backup>/qdrant-full.snapshot
```

Each collection is uploaded with `POST /collections/<c>/snapshots/upload?priority=snapshot`, which replaces that collection's data.

**Whole store from scratch, as an alternative:** Qdrant restores a full snapshot only at startup.
1. Stop `rmp-qdrant`.
2. Start the container once with `--storage-snapshot /path/to/qdrant-full.snapshot --force-snapshot`, and the snapshot mounted into it.
3. Restart it normally.

## Quarterly DR drill
1. Copy the latest backup to a staging VM, or to a scratch Postgres and Qdrant.
2. Run the restore script there.
3. Run `make production-check`.
4. Run the canary once.
