.PHONY: test backup readiness production-check go-live rollback verify-patch canary memory-canary janitor observability seed-vector-memory ensure-skills restart-rmp

test:
	cd /root/.openclaw/rmp && ./venv/bin/pytest tests/ -q

backup:
	bash /root/.openclaw/rmp/ops/backup.sh

readiness:
	cd /root/.openclaw/rmp && ./venv/bin/python -c "import asyncio,json; from app.production.readiness import run_all_checks; print(json.dumps(asyncio.run(run_all_checks()), indent=2))"

production-check:
	bash /root/.openclaw/rmp/ops/healthcheck.sh && bash /root/.openclaw/rmp/ops/ensure_openclaw_skills.sh && bash /root/.openclaw/rmp/ops/verify_openclaw_patch.sh && bash /root/.openclaw/rmp/ops/canary_intake_execution_mode.sh && bash /root/.openclaw/rmp/ops/canary_intake_latency.sh

go-live:
	bash /root/.openclaw/rmp/ops/go_live.sh

rollback:
	bash /root/.openclaw/rmp/ops/rollback_dev.sh

verify-patch:
	bash /root/.openclaw/rmp/ops/verify_openclaw_patch.sh

canary:
	bash /root/.openclaw/rmp/ops/canary.sh

canary-sentinel:
	bash /root/.openclaw/rmp/ops/canary_sentinel.sh scheduled

memory-canary:
	bash /root/.openclaw/rmp/ops/canary_slack_memory.sh

janitor:
	cd /root/.openclaw/rmp && ./venv/bin/python ops/workflow_janitor.py

seed-vector-memory:
	cd /root/.openclaw/rmp && ./venv/bin/python -m app.memory.seed

sync-nvidia-keys:
	/root/.openclaw/rmp/venv/bin/python /root/.openclaw/rmp/ops/sync_nvidia_keys.py

observability:
	bash /root/.openclaw/rmp/ops/start_observability.sh

ensure-skills:
	bash /root/.openclaw/rmp/ops/ensure_openclaw_skills.sh

restart-rmp:
	bash /root/.openclaw/rmp/ops/restart_rmp.sh

temporal-recover:
	bash /root/.openclaw/rmp/ops/temporal_recover.sh

temporal-health:
	bash /root/.openclaw/rmp/ops/temporal_healthcheck.sh

qdrant:
	bash /root/.openclaw/rmp/ops/start_qdrant.sh

migrate-qdrant:
	bash /root/.openclaw/rmp/ops/migrate_qdrant_to_server.sh
