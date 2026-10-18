#!/usr/bin/env python3
"""Sync LLM keys from /etc/openclaw/openclaw.env into OpenClaw auth store.

A failed sync is logged and never blocks a start (the units run it with '-'); exit 0 when any key synced,
NVIDIA or OpenAI, 1 when none did.
Never recreates leftover auth-profiles.json.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm.quota_broker import sync_nvidia_auth_profiles


def main() -> int:
    result = sync_nvidia_auth_profiles()
    print(json.dumps(result))
    return 0 if result.get("synced", 0) > 0 or result.get("openai_synced") else 1


if __name__ == "__main__":
    raise SystemExit(main())
