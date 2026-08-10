#!/usr/bin/env python3
"""Sync NVIDIA_API_KEY* from /etc/openclaw/openclaw.env into auth-profiles.json."""
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
