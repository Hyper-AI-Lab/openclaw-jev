# Go-Live Runbook

## Preconditions
- `make test` passes
- `make readiness` shows 0 blocking failures
- Backup completed: `make backup`

## Steps
1. `bash /root/.openclaw/rmp/ops/go_live.sh`
2. Verify: `curl -H "X-RMP-API-Key: $RMP_API_KEY" http://127.0.0.1:8000/api/production/readiness`
3. Monitor dashboard at http://127.0.0.1:8000/
4. Run canary: `bash /root/.openclaw/rmp/ops/canary.sh`

## Rollback
`bash /root/.openclaw/rmp/ops/rollback_dev.sh`

## 72h soak criteria
- No orphaned `running` tasks > 45 min without reconciler event
- Terminal failures have events + optional trace export
