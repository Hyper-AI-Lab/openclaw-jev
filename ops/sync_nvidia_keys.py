#!/usr/bin/env python3
"""Sync LLM keys from /etc/openclaw/openclaw.env into OpenClaw auth store.

Systemd ExecStartPre still invokes this path. NVIDIA keys remain required
for a successful pre-start; OPENAI_API_KEY is optional (warn-only).
Never recreates leftover auth-profiles.json.
"""
import json
import sys

sys.path.insert(0, "/root/.openclaw/rmp")

from app.llm.quota_broker import sync_nvidia_auth_profiles


def main() -> int:
    result = sync_nvidia_auth_profiles()
    print(json.dumps(result))
    return 0 if result.get("synced", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
