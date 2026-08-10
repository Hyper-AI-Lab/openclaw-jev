# OpenClaw Update Runbook

## After npm update
1. `npm update -g openclaw` (or project-specific update command)
2. `bash /root/.openclaw/rmp/patch_openclaw.sh`
3. `bash /root/.openclaw/rmp/ops/verify_openclaw_patch.sh`
4. `node /root/aura_safe_harbor/kairos_core/restore_powers.js`
5. `systemctl restart openclaw-gateway rmp-worker`
6. `bash /root/.openclaw/rmp/ops/canary.sh`

## If patch verify fails
Re-run patch script and inspect `/usr/lib/node_modules/openclaw/dist/` for upstream renames.
