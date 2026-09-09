# OpenClaw Update Runbook

Use the one-shot script. Do **not** run `openclaw onboard` or `openclaw update`. Do **not** pass `--force` to doctor. Do **not** hand-edit dist.

```bash
make -C /root/.openclaw/rmp upgrade-openclaw
# or:
bash /root/.openclaw/rmp/ops/upgrade_openclaw.sh
```

That is: backup → node check → `npm install -g openclaw@latest` → `doctor --fix --non-interactive` → restore RMP-critical `openclaw.json` keys and `TOOLS.md` → `patch_openclaw.sh` → `verify_openclaw_patch.sh` → skills → restart if no user tasks.

Then: `make -C /root/.openclaw/rmp production-check` and `RMP_CANARY_SKIP_SENTINEL=1 bash /root/.openclaw/rmp/ops/canary.sh`.

## If patch verify fails

Search `/usr/lib/node_modules/openclaw/dist` for the new symbol names and update `patch_openclaw.sh` only. Do not start an unpatched gateway. Rollback = reinstall the version in the backup `VERSIONS.txt`, restore `openclaw.json` + plugins, `patch_openclaw.sh`, restart.
