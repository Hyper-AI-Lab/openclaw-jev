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

```bash
bash /root/.openclaw/rmp/ops/restore_backup.sh /root/.openclaw/rmp/data/backups/YYYYMMDDTHHMMSSZ
```

The script does the following:
1. Asks for confirmation.
2. Stops rmp-api, rmp-worker and Temporal.
3. Restores `rmp_db`.
4. Restores both Temporal databases (`pg_restore --clean --if-exists`, as the postgres user).
5. Restores Qdrant collection by collection into the **running** Qdrant (`ops/restore_qdrant_snapshot.sh`).
6. Restores artifacts and settings.
7. Restarts the services.

The OpenClaw stores are restored separately, with the gateway stopped: `ops/backup_openclaw_state.py restore <backup>/openclaw-state --yes`.

If any part fails it prints `Restore INCOMPLETE: not restored: …` and exits 1.

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
