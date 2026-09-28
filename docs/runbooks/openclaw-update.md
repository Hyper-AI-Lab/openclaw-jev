# OpenClaw Update Runbook

Use the one-shot script. Do **not** run `openclaw onboard` or `openclaw update`. Do **not** pass `--force` to doctor. Do **not** hand-edit dist.

```bash
make -C /root/.openclaw/rmp upgrade-openclaw
# or:
bash /root/.openclaw/rmp/ops/upgrade_openclaw.sh
```

That is: backup (including each npm plugin's version in `VERSIONS.txt`) → node check → `npm install -g openclaw@latest` → `openclaw plugins update --all` (newest plugin versions compatible with the core; a Slack plugin left behind breaks the inbound debounce contract on every DM) → `doctor --fix --non-interactive` → restore RMP-critical `openclaw.json` keys and `TOOLS.md` → `patch_openclaw.sh` → `verify_openclaw_patch.sh` → skills → restart if no user tasks.

Then: `make -C /root/.openclaw/rmp production-check` and `RMP_CANARY_SKIP_SENTINEL=1 bash /root/.openclaw/rmp/ops/canary.sh`.

## If patch verify fails

Search `/usr/lib/node_modules/openclaw/dist` for the new symbol names and update `patch_openclaw.sh` only. Do not start an unpatched gateway. Rollback = reinstall the version in the backup `VERSIONS.txt`, `openclaw plugins install` each `plugin_before` spec, restore `openclaw.json` + plugins, `patch_openclaw.sh`, restart.
