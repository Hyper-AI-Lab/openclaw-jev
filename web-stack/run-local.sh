#!/usr/bin/env bash
# Local (non-Docker) runner for aura web-stack — useful for quick smoke tests.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT/backends"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -U pip
  .venv/bin/pip install -r requirements.txt
fi
export PYTHONPATH="$ROOT/backends"
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8791
