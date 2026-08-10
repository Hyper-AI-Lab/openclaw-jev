#!/usr/bin/env bash
# Idempotent schema migration for Phase 4 task registry tables/columns.
set -euo pipefail
exec bash /root/.openclaw/rmp/ops/migrate_task_registry_schema.sh
