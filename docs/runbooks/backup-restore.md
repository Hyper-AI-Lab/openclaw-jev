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
| `artifacts.tar.gz` | The evidence store: the artifact root the settings name (`artifact_store.root_path`), archived as one top-level directory named after the root, or, for a symlinked root, after the directory it resolves to. None while the store is disabled |
| `openclaw-state/` | OpenClaw's agent and state SQLite stores, taken with the backup API and checksummed. They hold OpenClaw's cron jobs too |
| `workspace.tar.gz` | Aura's workspace notes (`/root/.openclaw/workspace`), without `.git`, `moltmarket_venv`, `chat_logs` and `*.log` files. It holds the same classes of secrets as the OpenClaw store copies |
| `settings.json`, `openclaw.json` | Config snapshots |
| `manifest.json` | Format 2 (`app/production/backup_manifest.py`): `components` names each component's files, `files` gives each file's `bytes` and `sha256`, and `failures` is empty when every component succeeded. Backups made before it have no `format` and no file list |
| `backup.log` | The run's log |

**When a component fails:**
- Every other component still runs.
- A component also fails when one of its expected files is missing, empty or unreadable, or a dump or archive does not read back: each `*.dump` must read to its end with `pg_restore -f /dev/null`, and each `*.tar.gz` as a tar archive, gzip checksum included. With the artifact store disabled in settings, `artifacts` expects no file; with it enabled, a missing artifact root fails it.
- The failure is listed in `manifest.json` and in the log.
- The script exits 1, so `rmp-backup.service` shows as failed.
- **No older backup is pruned.**

Check the last run with `systemctl status rmp-backup.service`, then verify the newest backup against its manifest, checksums included. It prints one problem per line, and nothing when the backup is complete:

```bash
cd /root/.openclaw/rmp && venv/bin/python -m app.production.backup_manifest verify "$(ls -1d data/backups/2*/ | sort | tail -1)"
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
1. Verifies the backup against its manifest, checksums included (`python -m app.production.backup_manifest verify`). When it does not verify, it prints `WARNING: this backup does not verify:` and one line per problem. When the check itself cannot run, for example on a new host whose venv is not built yet, that line is `the check itself could not run (its error is on stderr)`, and the check's own error, on stderr, comes just before the warning. A damaged backup may be the only one left, so it warns rather than refuses. Then it asks for confirmation.
2. Takes the code-reload lock (`/run/rmp-code-reload.lock`) and holds it until it exits: a deploy waits for it, a code reload skips. It refuses before anything stops while the lock is held or an `aura-deploy-*` unit is active. The watchdog and the canary sentinel do not take the lock; step 3 stops their timers.
3. Stops every writer: each active `rmp-*` timer, then the service it triggers (a running canary, janitor, watchdog or backup run ends), then `rmp-code-watch` and `openclaw-gateway` when active, then `rmp-api`, `rmp-worker` and `temporal`. `rmp-qdrant` keeps running for step 6. Then it checks for a deploy unit again, because rmp-api or rmp-worker may have launched one before they stopped. A failed stop, a deploy unit or a signal ends the restore here: once it has stopped a unit, it prints `Restore ended before its first database step (exit <n>): nothing was restored`, starts again what it stopped and exits non-zero.
4. Restores `rmp_db` as an exact copy, through the postgres superuser (`ops/restore_pg_database.sh`). `pg_restore --clean --if-exists --create --exit-on-error -d postgres` drops the database and recreates it from its archive: its owner, encoding and settings, the `vector` extension, and nothing the dump lacks. The `rmp` role cannot do this: it has neither CREATEDB nor superuser. The first error stops it, so a `DROP DATABASE` refused because a session is still connected changes nothing.
5. Restores both Temporal databases the same way.
6. Restores Qdrant collection by collection into the **running** Qdrant (`ops/restore_qdrant_snapshot.sh`).
7. Restores artifacts, OpenClaw's stores and settings.
   - Artifacts go back to the artifact root the live settings name, which the step replaces whole. It refuses a root that is not absolute, is `/`, or is or holds the checkout, the data directory, the backup root or the backup being restored (each compared by `realpath -m`). It then prints `artifacts: refusing to replace <root>: <reason>; restore artifacts.tar.gz by hand`, touches nothing under the root, and `artifacts` is not restored. To restore it by hand, `tar -tzf <backup>/artifacts.tar.gz | head -1` names the archive's one top-level directory, and `tar -xzf <backup>/artifacts.tar.gz -C <directory>` extracts it into the directory that should hold it. Otherwise it extracts the archive beside the root, checks that it holds exactly one top-level directory, removes the root and moves that directory into its place, whatever the root was called when the backup was made. A symlinked root keeps its link: the directory it resolves to is replaced.
   - The stores (sessions, transcripts, auth profiles, cron jobs) are coupled to `rmp_db`, so they come back with it. `ops/backup_openclaw_state.py restore` checks every copy's checksum and integrity before it replaces anything, and refuses while the gateway runs.
8. When every part was restored, starts what it stopped: `temporal`, `rmp-api`, `rmp-worker`, then the gateway and `rmp-code-watch`, then the timers. Then it runs the timer check, one `next: <timer>` line per timer. A timer whose interval passed during the restore runs its service at once and shows no next elapse until that run ends, so the restore waits up to 15 minutes for such a run. A `NO NEXT: <timer>` line left makes it exit 1. Then wait for the run to end, or re-arm the timer: `systemctl start <timer>`, and if it still has no next elapse, `systemctl start <stem>.service` once, in an idle window. The check is `systemctl list-timers 'rmp-*' --all --no-pager --no-legend | awk '{print ($1 == "-" ? "NO NEXT: " : "next: ") $(NF-1)}'`.

**Not restored by the script:**
- `openclaw.json`. A verbatim copy undoes what RMP applied to it since the backup, so put it back by hand only when the live file is lost or damaged, then apply RMP's model policy to it again:

  ```bash
  cp -a <backup>/openclaw.json /root/.openclaw/openclaw.json
  cd /root/.openclaw/rmp && venv/bin/python -c 'from app.llm.model_policy import apply_openclaw_policy; print(apply_openclaw_policy()["changed"])'
  ```
- `workspace.tar.gz`. The workspace notes are Aura's own working files, which a restore of the databases should not roll back. Put them back by hand when the live workspace is lost or damaged; files the archive holds replace the live ones, and the rest stay:

  ```bash
  tar -xzf <backup>/workspace.tar.gz -C /root/.openclaw
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

**Whole store from scratch, as an alternative:** Qdrant restores a full snapshot only at startup. This needs Kirill's OK and an idle window, because Aura's memory is offline throughout; recall falls back to Postgres full-text search. Several things would otherwise start `rmp-qdrant` in the middle of it, and its start recreates the container on the storage being rebuilt:
- the canary sentinel, which restarts an inactive `rmp-qdrant` whenever a run finds a canary issue. It runs from its own timer, and from the health canary and the memory canary when they fail, which they do while Qdrant is down;
- any start or restart of `rmp-api` or `rmp-worker`, which want `rmp-qdrant` (`Wants=`): the Temporal watchdog's recovery, a deploy, an automatic restart after a crash, or a start of a unit that wants them in turn (`openclaw-gateway`, `rmp-code-watch`, whose code reload restarts both);
- a boot, `make qdrant` or `make migrate-qdrant`, or a `systemctl start` or `restart` of `rmp-qdrant` by hand.

So step 1 stops every `rmp-*` timer, as the restore does, which also keeps a backup from taking a snapshot of the storage; merge nothing that deploys before step 4 has ended. And while the one-off container `rmp-qdrant-from-snapshot` of step 3 exists, `ops/start_qdrant.sh` refuses to start `rmp-qdrant`, whatever asks for it: the unit then shows `failed`, and step 4's start works all the same. Step 3 creates the one-off before it checks that `rmp-qdrant` is stopped, and starts it only after that check, so no start of `rmp-qdrant` through systemd puts two Qdrants on the storage, whenever it comes. Run no `make qdrant`, `make migrate-qdrant` or `ops/start_qdrant.sh` by hand during the procedure: `systemctl is-active` does not see such a start, and one that passed its refusal check just before the create still runs its container.

1. Run the timer check, stop every `rmp-*` timer, and ask whether any of their services is running:
   ```bash
   systemctl list-timers 'rmp-*' --all --no-pager --no-legend | awk '{print ($1 == "-" ? "NO NEXT: " : "next: ") $(NF-1)}'
   systemctl stop rmp-backup.timer rmp-canary-sentinel.timer rmp-canary.timer rmp-janitor-frequent.timer rmp-janitor.timer rmp-memory-canary.timer rmp-temporal-vacuum.timer rmp-temporal-watchdog.timer
   systemctl is-active rmp-backup.service rmp-canary-sentinel.service rmp-canary.service rmp-janitor-frequent.service rmp-janitor.service rmp-memory-canary.service rmp-temporal-vacuum.service rmp-temporal-watchdog.service
   ```
   All are oneshot services, which show `activating` while a run is in progress. If any line is `activating` or `active`, wait for that run to end and ask again: a canary run in progress may still call the sentinel at its end. Then run the timer check again: every line shows `NO NEXT:`.
2. Stop Qdrant. This stops the container `rmp-qdrant`, whose data stays in the bind mount. Then check the container itself, because `systemctl stop` reports success even when the unit's stop script fails (the unit then shows `failed`):
   ```bash
   systemctl stop rmp-qdrant
   docker inspect -f '{{.State.Running}}' rmp-qdrant
   ```
   It must print `false`, or fail with no such object where `rmp-qdrant` does not exist (step 3 says what to do then). If it prints `true`, Qdrant still runs on the storage: do not go on. `journalctl -u rmp-qdrant -n 20` shows why the stop failed; fix that, run `docker stop rmp-qdrant` and check again.
3. Restore the storage with a one-off container named `rmp-qdrant-from-snapshot`, never `rmp-qdrant`. It mounts the same storage directory and the snapshot, publishes no port, and runs the image on `ops/start_qdrant.sh`'s `IMAGE=` line, the one step 4 runs on the restored storage, which the first line reads and prints. Create it first: from then on `ops/start_qdrant.sh` refuses to start `rmp-qdrant`. Then ask whether `rmp-qdrant` runs or is starting, which something may have caused since step 2:
   ```bash
   image="$(sed -n 's/^IMAGE="\(.*\)"$/\1/p' /root/.openclaw/rmp/ops/start_qdrant.sh)"; echo "${image}"
   docker create --name rmp-qdrant-from-snapshot \
     -v /root/.openclaw/rmp/data/qdrant-server:/qdrant/storage \
     -v <backup>/qdrant-full.snapshot:/qdrant/full.snapshot:ro \
     "${image}" \
     ./qdrant --storage-snapshot /qdrant/full.snapshot --force-snapshot
   systemctl is-active rmp-qdrant
   docker inspect -f '{{.State.Running}}' rmp-qdrant
   ```
   If the `docker create` fails, it created nothing: fix what it says and run the whole block again, so that both questions come after a create that succeeded. If it fails because a container named `rmp-qdrant-from-snapshot` exists, left by an earlier attempt or a reboot, remove that with `docker rm rmp-qdrant-from-snapshot` and run the whole block again.

   `systemctl is-active` must print `inactive` or `failed`, and `docker inspect` `false`, or fail with no such object. Anything else means a Qdrant runs or is starting on the storage. If `systemctl is-active` prints `active` or `activating`, run `systemctl stop rmp-qdrant`. If `docker inspect` still prints `true`, the container runs without systemd knowing of it (a start by hand, or one whose start systemd cut short): run `docker stop rmp-qdrant`. Then ask both again. The one-off never starts beside a running Qdrant. Then start the one-off:
   ```bash
   docker start rmp-qdrant-from-snapshot
   docker logs rmp-qdrant-from-snapshot 2>&1 | grep -c 'Qdrant HTTP listening on 6333'
   ```
   It is ready when the `docker logs` line prints `1`; repeat that line until it does. Then remove it:
   ```bash
   docker stop rmp-qdrant-from-snapshot && docker rm rmp-qdrant-from-snapshot
   ```
   If `docker inspect -f '{{.State.Status}}' rmp-qdrant-from-snapshot` prints `exited` before that line appears, the restore failed, and the storage may hold some collections from the snapshot and not others: `docker logs rmp-qdrant-from-snapshot` says why. Remove the container with the same command and run the whole first block of this step again, with the same backup or an older one, so that both questions come after the create again. Go on to step 4 only after the `docker logs` line printed `1`. If the host reboots during the procedure, start again at step 1, because a boot arms the timers again; step 3 then finds the old one-off as above.
4. Start Qdrant, which recreates `rmp-qdrant` on the restored storage. Then readiness shows `vector_memory`, `task_registry_vector` and `vector_sync` at `pass`:
   ```bash
   systemctl start rmp-qdrant
   cd /root/.openclaw/rmp && make -s readiness | python3 -c 'import json,sys; d=json.load(sys.stdin); print({c["name"]: c["status"] for c in d["checks"] if c["name"] in ("vector_memory","task_registry_vector","vector_sync")})'
   ```
5. Start the timers again, then run the timer check: 8 `next:` lines.
   ```bash
   systemctl start rmp-backup.timer rmp-canary-sentinel.timer rmp-canary.timer rmp-janitor-frequent.timer rmp-janitor.timer rmp-memory-canary.timer rmp-temporal-vacuum.timer rmp-temporal-watchdog.timer
   systemctl list-timers 'rmp-*' --all --no-pager --no-legend | awk '{print ($1 == "-" ? "NO NEXT: " : "next: ") $(NF-1)}'
   ```
   A timer whose interval passed meanwhile runs its service at once and shows `NO NEXT:` until that run ends: wait for it and run the check again. A timer still without a next elapse is re-armed as in step 8 of the restore above.

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

**First it verifies the backup** against its manifest, every file's size and checksum. A problem is a `FAIL backup manifest: …` line, and the drill stops there (exit 1), before it creates anything.

**What it does then:**
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
