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
| `manifest.json` | Format 2 (`app/production/backup_manifest.py`): `components` names each component's files, `files` gives each file's `bytes` and `sha256`, `offsite` says whether the backup was copied off the host (`off`, `copied` or `failed`; see "Off-host copy" below), and `failures` is empty when every component succeeded. Backups made before it have no `format` and no file list |
| `backup.log` | The run's log |

**When a component fails:**
- Every other component still runs.
- A component also fails when one of its expected files is missing, empty or unreadable, or a dump or archive does not read back: each `*.dump` must read to its end with `pg_restore -f /dev/null`, and each `*.tar.gz` as a tar archive, gzip checksum included. With the artifact store disabled in settings, `artifacts` expects no file; with it enabled, a missing artifact root fails it.
- The failure is listed in `manifest.json` and in the log.
- With the off-host copy switched on, a copy that fails, or an env file that is missing, belongs to another user or is readable by others, is the failure `offsite`. So is a `backup.offsite` setting that cannot be read, such as `"offsite": true` instead of an object.
- The script exits 1, so `rmp-backup.service` shows as failed.
- **No older backup is pruned.**
- **Kirill gets one ops DM for each failed run**, from `rmp-unit-failed@rmp-backup.service.service`, which the unit's `OnFailure=` starts. A run that times out (`TimeoutStartSec=1h`) fails the unit too. The DM (`ops/notify_unit_failure.py`) names the UTC date and the run, the newest backup directory when it is less than 2 hours old: its stamp and the components its manifest lists as failed, or that the run left no manifest. A manifest that lists none means the run failed after writing it. The DM holds no log line.
  - The same alert for the same run is not sent twice. A failed re-run on the same day that writes its own backup directory is another run, and sends its own DM.
  - A run can fail before it writes its directory: another backup holds the lock (its journal says `another backup is running`), or the directory cannot be created. Its DM then describes the newest directory less than 2 hours old, which can be a run still in progress (said to have left no manifest), or says there is none. When an earlier alert of that day had the same text, no DM goes out.
  - A DM that did not go out leaves the alert unit failed: `systemctl --failed` lists it, and `journalctl -u rmp-unit-failed@rmp-backup.service.service` shows the alert's text.

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

**Qdrant's API key:** Qdrant refuses a request without its key, `QDRANT_API_KEY` in `/etc/rmp/rmp.env`. The backup and the restore send it through `ops/qdrant_curl.sh`, and every call to Qdrant by hand goes through it too. It takes curl's arguments and sends the key on curl's stdin, never on a command line, which any local user can read:

```bash
cd /root/.openclaw/rmp && bash ops/qdrant_curl.sh -fsS http://127.0.0.1:6333/collections
```

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
3. Restore the storage with a one-off container named `rmp-qdrant-from-snapshot`, never `rmp-qdrant`. It mounts the same storage directory and the snapshot, publishes no port, and runs the image on `ops/start_qdrant.sh`'s `IMAGE=` line, the one step 4 runs on the restored storage, which the first line reads and prints. It joins Docker's default bridge like `rmp-qdrant`, so it requires the same key, and no copy of Aura's memory without the key appears there: the second line puts the key into this shell's environment, read as `ops/start_qdrant.sh` reads it, and `docker create` takes its value from there (`-e QDRANT__SERVICE__API_KEY`), so only its name is on a command line. That line prints the key's length, `64`, never the key; `0` would mean that `/etc/rmp/rmp.env` holds no `QDRANT_API_KEY`, so that `rmp-qdrant` has none either. Create the one-off first: from then on `ops/start_qdrant.sh` refuses to start `rmp-qdrant`. Then ask whether `rmp-qdrant` runs or is starting, which something may have caused since step 2:
   ```bash
   image="$(sed -n 's/^IMAGE="\(.*\)"$/\1/p' /root/.openclaw/rmp/ops/start_qdrant.sh)"; echo "${image}"
   export QDRANT__SERVICE__API_KEY="$(cd /root/.openclaw/rmp && venv/bin/python -c 'from app.memory.vector import qdrant_api_key; print(qdrant_api_key())')"; echo "${#QDRANT__SERVICE__API_KEY}"
   docker create --name rmp-qdrant-from-snapshot \
     -e QDRANT__SERVICE__API_KEY \
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
4. Remove the key from this shell's environment, and start Qdrant, which recreates `rmp-qdrant` on the restored storage. Then readiness shows `vector_memory`, `task_registry_vector` and `vector_sync` at `pass`:
   ```bash
   unset QDRANT__SERVICE__API_KEY
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

`ops/restore_drill.py` copies a backup through a scratch restic repository, restores the copy into a scratch PostgreSQL and a scratch Qdrant, checks every store, and removes all of it. It restores nothing in production and only reads production's databases: the idle check counts the active user tasks, and one query lists the role names. Run it quarterly, and after any change to a backup or restore script, as root, on an idle host, outside the backup's window (03:00-03:45 UTC):

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
2. The restic round trip, the off-host step on real data. In the scratch directory's `restic/` (mode 0700), a repository whose password is random and lives only in a 0600 file there, then `restic init`, `bash ops/offsite_copy.sh <backup>` (the step `ops/backup.sh` runs) and `restic restore latest --target <scratch>/from-restic`. The restored copy, under `from-restic/` at the backup's own path, must match the backup's manifest, checksums included: `ok restic round trip: <n> files, checksums match`. Every step after this one reads the restored copy. The drill never reads `/etc/rmp/restic.env`, and no `RESTIC_` variable of the caller's environment reaches restic. A missing restic, a failed step or a copy that does not verify is a `FAIL` line, and the steps after it then read the backup itself. The repository and the restored copy each take about the backup's size in `/var/tmp` until the cleanup.
3. A throwaway PostgreSQL 16 from the host's own binaries (`/usr/lib/postgresql/16/bin`): `initdb` and `pg_ctl` as postgres, socket only in `pg/`, port 55432, no TCP listener. Production's roles (today `rmp` and `temporal`) are created there `NOLOGIN`, so the archives' owners apply.
4. A container `rmp-qdrant-drill` of the image `ops/start_qdrant.sh` runs, on `127.0.0.1:6937`, with 1 GiB and no volume. It requires a key made for this drill, which docker and the snapshot restore get through their environment and which is never printed. Port 6937 is in `coding.blocked_tcp_ports`, so aura-coder cannot reach the copy of Aura's memory, and `coding_isolation` in `make readiness` stays at `pass` while the drill runs.
5. Restores `rmp_db`, `temporal` and `temporal_visibility` through `ops/restore_pg_database.sh`, the restore's own command, and the Qdrant snapshot through `ops/restore_qdrant_snapshot.sh`.
6. Checks:
   - each database's owner: `rmp_db` → `rmp`, `temporal` and `temporal_visibility` → `temporal`;
   - the `vector` extension in `rmp_db`;
   - every table's `count(*)` against the rows of its `COPY` block in `pg_restore --data-only -f - <dump>`;
   - the collections against the snapshot, with each collection's points;
   - OpenClaw's store copies (`backup_openclaw_state.verify`);
   - every `*.tar.gz` read to its end.

**Output:** one line per step and check, `ok …` or `FAIL <reason>`, with names and counts only. Expected: every line `ok`, the restic round trip included. The exit code is 0 only when every check passed. The last line is `restore duration: <m> min <s> s`, the time from the first database restore to the end of the Qdrant restore: the steps a real restore repeats on the same data, so it is the basis of the RTO (see "Off-host copy" below).

**Cleanup:** however the drill ends, including a SIGTERM, Ctrl-C or a hung-up session, it runs `docker rm -f rmp-qdrant-drill`, `pg_ctl stop -m fast` and removes the scratch directory. Started under `nohup`, which ignores the hangup, it keeps running when the session ends (redirect its output, or nohup writes `nohup.out` into the live checkout). If the drill is killed hard (`kill -9`, a reboot), remove what it left:

```bash
docker rm -f rmp-qdrant-drill; for d in /var/tmp/rmp-restore-drill-*; do sudo -n -u postgres /usr/lib/postgresql/16/bin/pg_ctl -D "$d/pg/data" stop -m fast; rm -rf "$d"; done
```

A drill with a `FAIL` line means a store does not restore from that backup: diagnose it before relying on the backup, and before any change that needs a proven restore.

## Off-host copy (accepted exception K-7)

Backups stay on this host, by Kirill's decision: an accepted exception (K-7). A loss of this disk loses every backup.

The step is built and tested, and switched off. When settings' `backup.offsite.enabled` is `true`, `ops/backup.sh` runs `ops/offsite_copy.sh <backup dir>` once the manifest is written:
- `restic backup --tag rmp-backup --host <host>` copies the directory into the repository, encrypted on this host;
- `restic forget --tag rmp-backup --host <host> --group-by host --keep-last 14 --prune` then keeps the newest 14 copies this host made. Grouped by host and path, restic's default, each night's directory would be a group of its own, and nothing would ever be forgotten.

Its configuration lives only in `/etc/rmp/restic.env`: `RESTIC_REPOSITORY`, `RESTIC_PASSWORD_FILE` and the backend's variables, never in `settings.json`, which `GET /settings` returns. The backup reads the file only when it belongs to the user the backup runs as (root) and has mode 0600, and only in a subshell, so its variables reach restic and no other step; restic's output goes to `backup.log`. A missing file, a file with another owner or mode, a failed copy, or a `backup.offsite` setting that cannot be read fails the run as `offsite`. The copy runs after a local failure too. Each manifest's `offsite` says `off`, `copied` or `failed`; on this host it says `off`.

The drill copies each drilled backup through restic into a scratch repository and drills the copy restic restores ("Quarterly DR drill" above). From WP-12 batch 7, readiness reports the exception as the warning `offsite_backup`, the one warning the soak accepts (K-24).

**RPO, RTO and retention:**
- **RPO: 24 hours.** One backup runs a day, at 03:15 UTC (`rmp-backup.timer`), so a restore loses at most the day since then. This holds against damage inside the host's stores: corruption, a bad migration, an operator error. Against the loss of this disk there is no recovery point (K-7).
- **RTO: 1 minute.** The drill's `restore duration` was 0 min 7 s on 2026-10-19 and 0 min 13 s on 2026-10-21 (after WP-12 batch 4), rounded up to whole minutes. It covers the restore of the three databases and of Qdrant on today's data. It leaves out stopping and starting the services, the artifacts and OpenClaw's stores, and the operator's own time. Each quarterly drill prints the duration again; update this number when it changes.
- **Retention:** the newest 14 complete backups on this host (`KEEP=14` in `ops/backup.sh`; "Retention" above). Off the host, when switched on, the newest 14 copies (`--keep-last 14`), complete or not. The copies of failed runs count towards them, so 14 failed nights in a row would leave no complete backup off the host, while this host still keeps its last 14.

**To switch it on** (Kirill decides the destination and who holds the key):
1. Write `/etc/rmp/restic.env`, owned by root with mode 0600: `RESTIC_REPOSITORY`, `RESTIC_PASSWORD_FILE` (a root-only 0600 file that holds the password) and the backend's variables.
2. Initialise the repository with it, which keeps every value off the command line: `( set -a; . /etc/rmp/restic.env; set +a; restic init )`.
3. Keep a copy of the password off the host. Without it the copies cannot be read, and a lost disk takes the password file with it.
4. Set `backup.offsite.enabled` to `true` in `settings.json`. The next nightly manifest says `"offsite": "copied"`. From then on a failed copy fails the night's run as `offsite`, however complete the backup on this host: while the repository cannot be reached, no new backup counts as complete, so this host prunes nothing and keeps one more backup each night, and the drill by default drills the last complete backup from before the outage.

To restore from the copy, with `/etc/rmp/restic.env` and the password file in place, run each restic command below as `( set -a; . /etc/rmp/restic.env; set +a; restic … )`. The newest copy is not always the one to restore: a failed run is copied too.
1. `restic snapshots --tag rmp-backup` lists the copies, oldest first, each with its id and its backup's path.
2. `restic dump <id> <path>/manifest.json` prints a copy's manifest. Pick the newest copy whose `failures` is `[]`.
3. `restic restore <id> --target <dir>`. The backup comes back under `<dir>` at its own path, and `ops/restore_backup.sh` restores it from there, verifying it first, as above.
