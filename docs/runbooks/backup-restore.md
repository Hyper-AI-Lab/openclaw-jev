# Backup & Restore Runbook

## Daily backup
Automated via `rmp-backup.timer` at 03:15 UTC.

Manual: `bash /root/.openclaw/rmp/ops/backup.sh`

Backups stored in `/root/.openclaw/rmp/data/backups/<timestamp>/`:
- `rmp_db.dump` — PostgreSQL
- `qdrant.tar.gz` — vector memory
- `artifacts.tar.gz` — evidence store
- `temporal.db` — workflow persistence
- `settings.json`, `openclaw.json`, `cron_jobs.json`

## Restore
```bash
bash /root/.openclaw/rmp/ops/restore_backup.sh /root/.openclaw/rmp/data/backups/YYYYMMDDTHHMMSSZ
```

## Quarterly DR drill
1. Copy latest backup to staging VM
2. Run restore script
3. `make production-check`
4. Run canary once
