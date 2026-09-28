#!/usr/bin/env python3
"""Print LLM usage per key and per category (requests + tokens, incl. OpenClaw transcript scrape)."""
import json
import sys

sys.path.insert(0, "/root/.openclaw/rmp")

from app.llm.usage_monitor import get_summary, scrape_openclaw_sessions


def main() -> int:
    scrape = scrape_openclaw_sessions()
    summary = get_summary()
    out = {"scrape": scrape, **summary}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
