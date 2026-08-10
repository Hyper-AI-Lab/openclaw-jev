#!/usr/bin/env python3
"""Probe Temporal dev server; optionally restart on failure."""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time


async def probe_temporal(timeout_sec: float = 8.0) -> tuple[bool, str]:
    from temporalio.client import Client

    try:
        client = await asyncio.wait_for(
            Client.connect("localhost:7233"), timeout=timeout_sec
        )
    except Exception as exc:
        return False, f"connect failed: {exc}"

    try:
        count = 0
        async for _ in client.list_workflows('ExecutionStatus="Running"'):
            count += 1
            break
    except Exception as exc:
        return False, f"list_workflows failed: {exc}"

    return True, f"ok (sample running workflows visible={count >= 0})"


def restart_temporal_stack() -> None:
    subprocess.run(
        ["systemctl", "restart", "temporal-dev.service"],
        check=False,
    )
    time.sleep(5)
    subprocess.run(
        ["systemctl", "restart", "rmp-worker.service", "rmp-api.service"],
        check=False,
    )
    time.sleep(3)


def main() -> int:
    parser = argparse.ArgumentParser(description="Temporal health probe")
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Restart temporal-dev (+ rmp api/worker) if probe fails once",
    )
    args = parser.parse_args()

    ok, msg = asyncio.run(probe_temporal())
    if ok:
        print(f"Temporal health: {msg}")
        return 0

    print(f"Temporal health FAIL: {msg}", file=sys.stderr)
    if args.recover:
        print("Attempting temporal-dev restart...", file=sys.stderr)
        restart_temporal_stack()
        ok2, msg2 = asyncio.run(probe_temporal())
        if ok2:
            print(f"Temporal recovered: {msg2}")
            return 0
        print(f"Temporal still unhealthy: {msg2}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
