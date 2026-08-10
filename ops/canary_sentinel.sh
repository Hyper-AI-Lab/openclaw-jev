#!/usr/bin/env bash
# Evaluate canary result files; auto-remediate or Slack-alert on failure.
set -euo pipefail
cd /root/.openclaw/rmp
exec ./venv/bin/python -m app.production.canary_sentinel --trigger "${1:-scheduled}"
