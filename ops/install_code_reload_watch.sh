#!/usr/bin/env bash
# Install recursive code watcher (replaces non-recursive systemd.path approach).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v inotifywait >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq inotify-tools
fi

chmod +x \
  "${ROOT}/ops/watch_code_and_reload.sh" \
  "${ROOT}/ops/reload_runtime_on_code_change.sh" \
  "${ROOT}/ops/install_code_reload_watch.sh"

install -m 644 "${ROOT}/ops/systemd/rmp-code-watch.service" /etc/systemd/system/rmp-code-watch.service
install -m 644 "${ROOT}/ops/systemd/rmp-code-reload.service" /etc/systemd/system/rmp-code-reload.service

systemctl daemon-reload
# Disable non-recursive path unit if present
systemctl disable --now rmp-code-reload.path 2>/dev/null || true
systemctl enable --now rmp-code-watch.service
systemctl is-active rmp-code-watch.service
echo "Installed: rmp-code-watch.service (inotify recursive on ${ROOT}/app)"
