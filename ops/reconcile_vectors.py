"""Diff Postgres memory and the task registry with their Qdrant indexes; optionally repair.

Dry run by default. --apply queues missing rows for the vector outbox, deletes points no
row references, and drains the outbox until it is empty (or --drain-max rounds).

    venv/bin/python -m ops.reconcile_vectors [--apply] [--drain-max 400]
"""
from __future__ import annotations

import argparse
import asyncio
import json

from app.memory.vector_sync import drain_once, reconcile


async def main(apply: bool, drain_max: int) -> None:
    stats = await reconcile(apply=apply)
    print(json.dumps(stats, indent=2))
    if not apply:
        print("dry run: nothing changed (use --apply)")
        return
    done = failed = 0
    for _ in range(drain_max):
        result = await drain_once(limit=100)
        done, failed = done + result["done"], failed + result["failed"]
        if not result["done"] and not result["failed"]:
            break
    print(f"outbox drained: {done} indexed, {failed} failed (failed rows retry with backoff)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="repair (default: dry run)")
    parser.add_argument("--drain-max", type=int, default=400, help="max drain rounds of 100")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.drain_max))
