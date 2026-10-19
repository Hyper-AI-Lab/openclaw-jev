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

Run it inside `tmux` or `screen`. An SSH session that hangs up then neither ends the restore nor loses its output, and after a partial failure that output holds the one command that starts the units again.

**On a new host**, the Postgres roles `rmp` and `temporal` must exist before the restore, because each archive names its owner. No backup holds them or their passwords. Create each as the postgres superuser with the password from `/etc/rmp/rmp.env` (`DATABASE_URL`) or `/etc/openclaw/openclaw.env`; the prompt keeps it off the command line:

```bash
sudo -u postgres createuser --pwprompt rmp
sudo -u postgres createuser --pwprompt temporal
```

The script does the following:
1. Asks for confirmation.
2. Takes the code-reload lock (`/run/rmp-code-reload.lock`) and holds it until it exits: a deploy waits for it, a code reload skips. It refuses before anything stops while the lock is held or an `aura-deploy-*` unit is active. The watchdog and the canary sentinel do not take the lock; step 3 stops their timers.
3. Stops every writer: each active `rmp-*` timer, then the service it triggers (a running canary, janitor, watchdog or backup run ends), then `rmp-code-watch` and `openclaw-gateway` when active, then `rmp-api`, `rmp-worker` and `temporal`. `rmp-qdrant` keeps running for step 6. Then it checks for a deploy unit again, because rmp-api or rmp-worker may have launched one before they stopped. A failed stop, a deploy unit or a signal ends the restore here: once it has stopped a unit, it prints `Restore ended before its first database step (exit <n>): nothing was restored`, starts again what it stopped and exits non-zero.
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

**If a part fails** after the first database step, or a signal ends the restore after that step and before its last (Ctrl-C, a `kill`, or an SSH session that hangs up), the restore starts nothing, because a store may be half restored: rmp-api runs `create_all` on `rmp_db` at start, and the watchdog would restart Temporal on a partial database. It exits non-zero and prints:
- `Restore INCOMPLETE: not restored: …` for a failed part, and `Restore INCOMPLETE: it ended before its last step` when a signal ended it;
- the units it left stopped, and the one `systemctl start` command that starts them in order;
- a warning not to start rmp-api, rmp-worker or openclaw-gateway, run a deploy or reboot before the stores are whole.

Make the stores whole, by restoring the failed part again or running the whole restore again, then run the printed `systemctl start` command. A second run of the restore starts only what it stopped itself, which after a first run that left everything stopped is `temporal`, `rmp-api` and `rmp-worker`. So the first run's command is still needed: it starts the gateway and `rmp-code-watch` and arms the timers. Until then, the second run's timer check prints `NO NEXT:` for every timer the first run stopped, and the second run exits 1 without `Restore complete.`; that is expected. Run the timer check again after the command.

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

`ops/restore_drill.py` restores a backup into a scratch PostgreSQL and a scratch Qdrant, checks every store, and removes both. It restores nothing in production and only reads production's databases: the idle check counts the active user tasks, and one query lists the role names. Run it quarterly, and after any change to a backup or restore script, as root, on an idle host, outside the backup's window (03:00-03:45 UTC):

```bash
cd /root/.openclaw/rmp && venv/bin/python ops/restore_drill.py
```

It drills the newest backup whose manifest has `"failures": []`; to drill another, pass its directory as the argument.

**It refuses to start** (exit 2, nothing created), printing one `refused:` line per reason, when:
- it does not run as root;
- `MemAvailable` is below 2 GiB;
- a user task is active, or the count fails (`count_active_user_tasks_sync(strict=True)`);
- an `aura-claude-*`, `aura-verify-*`, `aura-collect-*`, `aura-direct-*` or `aura-deploy-*` unit is active or activating;
- port 6937 is not in `coding.blocked_tcp_ports`;
- something accepts connections on `127.0.0.1:6937`;
- a container named `rmp-qdrant-drill` exists.

**What it does:**
1. A scratch directory `/var/tmp/rmp-restore-drill-*` (mode 0711), with `pg/` owned by postgres (mode 0700).
2. A throwaway PostgreSQL 16 from the host's own binaries (`/usr/lib/postgresql/16/bin`): `initdb` and `pg_ctl` as postgres, socket only in `pg/`, port 55432, no TCP listener. Production's roles (today `rmp` and `temporal`) are created there `NOLOGIN`, so the archives' owners apply.
3. A container `rmp-qdrant-drill` of the image `ops/start_qdrant.sh` runs, on `127.0.0.1:6937`, with 1 GiB and no volume. It requires a key made for this drill, which docker and the snapshot restore get through their environment and which is never printed. Port 6937 is in `coding.blocked_tcp_ports`, so aura-coder cannot reach the copy of Aura's memory, and `coding_isolation` in `make readiness` stays at `pass` while the drill runs.
4. Restores `rmp_db`, `temporal` and `temporal_visibility` through `ops/restore_pg_database.sh`, the restore's own command, and the Qdrant snapshot through `ops/restore_qdrant_snapshot.sh`.
5. Checks:
   - each database's owner: `rmp_db` → `rmp`, `temporal` and `temporal_visibility` → `temporal`;
   - the `vector` extension in `rmp_db`;
   - every table's `count(*)` against the rows of its `COPY` block in `pg_restore --data-only -f - <dump>`;
   - the collections against the snapshot, with each collection's points;
   - OpenClaw's store copies (`backup_openclaw_state.verify`);
   - every `*.tar.gz` read to its end.

**Output:** one line per step and check, `ok …` or `FAIL <reason>`, with names and counts only. Expected: every line `ok`. The exit code is 0 only when every check passed. The last line is `restore duration: <m> min <s> s`, the time from the first database restore to the end of the Qdrant restore: the steps a real restore repeats on the same data, so it is the basis of the RTO.

**Cleanup:** however the drill ends, including a SIGTERM, Ctrl-C or a hung-up session, it runs `docker rm -f rmp-qdrant-drill`, `pg_ctl stop -m fast` and removes the scratch directory. Started under `nohup`, which ignores the hangup, it keeps running when the session ends (redirect its output, or nohup writes `nohup.out` into the live checkout). If the drill is killed hard (`kill -9`, a reboot), remove what it left:

```bash
docker rm -f rmp-qdrant-drill; for d in /var/tmp/rmp-restore-drill-*; do sudo -n -u postgres /usr/lib/postgresql/16/bin/pg_ctl -D "$d/pg/data" stop -m fast; rm -rf "$d"; done
```

A drill with a `FAIL` line means a store does not restore from that backup: diagnose it before relying on the backup, and before any change that needs a proven restore.
