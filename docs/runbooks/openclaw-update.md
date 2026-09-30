# OpenClaw Update Runbook

Use the one-shot script. Do **not** run `openclaw onboard` or `openclaw update`. Do **not** pass `--force` to doctor. Do **not** hand-edit dist.

## 1. Rehearse on staging

Build a staging gateway on a copy of production's data and probe it. It runs on port 19789 under `/srv/openclaw-staging`, with Slack and cron off, and cannot write to production:

```bash
cd /root/.openclaw/rmp
venv/bin/python ops/openclaw_staging.py node                     # Node 24 LTS in /opt, if the target needs it
venv/bin/python ops/openclaw_staging.py build --version 2026.9.7
venv/bin/python ops/openclaw_staging.py start                    # prints the time to /readyz
venv/bin/python ops/openclaw_probe.py --target staging --boot-seconds <S>
ROLLBACK_TARGET=staging bash ops/rollback_openclaw.sh            # rehearse the way back
venv/bin/python ops/openclaw_probe.py --target staging
venv/bin/python ops/openclaw_staging.py stop
```

The probe suite must be green on the new version and again after the rehearsed rollback.

## 2. Upgrade production

Announce it first, and run it only with no user task active. Pin the version you rehearsed:

```bash
OPENCLAW_PACKAGE=openclaw@2026.9.7 bash /root/.openclaw/rmp/ops/upgrade_openclaw.sh
```

The script runs these steps in order:
1. The pre-flight rehearses the target in a scratch directory.
2. Backup: online copies of both SQLite stores, `openclaw.json`, the local plugins, `TOOLS.md` and `AGENTS.md`, and `VERSIONS.txt` with each npm plugin's version.
3. The Node check (≥ 24.16.0).
4. `npm install -g`.
5. `openclaw plugins update --all`. A Slack plugin left behind breaks the inbound debounce contract on every DM.
6. `doctor --fix --non-interactive`.
7. RMP's model policy (including `utilityModel: ""`), config keys and `TOOLS.md` (2026.9.7's doctor archives it).
8. Settle the session rows.
9. The key sync.
10. `patch_openclaw.sh`.
11. Skills.
12. `verify_openclaw_patch.sh`.
13. A restart, if there are still no user tasks.

Then check:

```bash
venv/bin/python ops/openclaw_probe.py --target production --before-db <backup>/openclaw-state/openclaw-agent.sqlite
make -C /root/.openclaw/rmp production-check
RMP_CANARY_SKIP_SENTINEL=1 bash /root/.openclaw/rmp/ops/canary.sh
```

Then send Aura a real DM.

## If patch verify fails

The audit names every patch that is missing and every file still holding unpatched code. Search `/usr/lib/node_modules/openclaw/dist` for the new symbol names and update `patch_openclaw.sh` and the list in `ops/openclaw_patch_audit.py` together. Do not start an unpatched gateway.

## Rollback

```bash
bash /root/.openclaw/rmp/ops/rollback_openclaw.sh [BACKUP_DIR]   # default: the last upgrade's backup
```

It refuses while user tasks run (`--force` overrides). Then it runs these steps in order:
1. Stop the gateway.
2. Reinstall the version in `VERSIONS.txt`.
3. Restore both stores.
4. Restore `openclaw.json`, the local plugins and the workspace files.
5. Reinstall each npm plugin at its old version. 2026.9.7's update deletes the old plugin generations.
6. Settle, patch, audit.
7. Start, and wait for `/readyz`.
