#!/usr/bin/env python3
"""Replay the running workflows' histories on this tree's code, for the deploy (specs/WP-09.md WP09-24).

Run as ``<venv>/bin/python ops/replay_live.py`` with the tree as its working directory and ``PYTHONPATH``. The deploy
runs it before it changes anything, on the head's tree and on the live one: a workflow that replays on the live code and
not on the head's would wedge on the new code, so the deploy is blocked. It lists the running workflows of the types
``worker.WORKFLOWS`` registers, fetches each history and replays them with ``Replayer(workflows=WORKFLOWS)``, the
worker's own runner. It prints one JSON line, ``{"replayed": [ids], "failed": {id: the error's first line}}``, and
exits 0; when it cannot list or fetch, ``{"error": ...}`` and exit 2. Histories hold Kirill's messages, so they stay
in memory: only ids and one line of each error are printed.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Sequence, Tuple

# This tree's modules, whatever else is on the path.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from temporalio.client import WorkflowHistory  # noqa: E402
from temporalio.worker import Replayer  # noqa: E402

from app.temporal_control import connect_temporal  # noqa: E402
from worker import WORKFLOWS  # noqa: E402

RUNNING = 'ExecutionStatus="Running"'
ERROR_CHARS = 200


def first_line(exc: BaseException) -> str:
    """The first line of the error, at most ``ERROR_CHARS`` characters; its type when it says nothing."""
    text = str(exc).strip()
    return (text.splitlines()[0] if text else type(exc).__name__)[:ERROR_CHARS]


async def replay(histories: Iterable[WorkflowHistory], workflows: Sequence[type] = WORKFLOWS) -> Dict[str, Any]:
    """Replay each history on ``workflows``' code: the ids that replayed, and each one that failed with its error's
    first line."""
    histories = list(histories)
    replayed: List[str] = []
    failed: Dict[str, str] = {}
    if not histories:
        return {"replayed": replayed, "failed": failed}

    async def each() -> AsyncIterator[WorkflowHistory]:
        for history in histories:
            yield history

    async with Replayer(workflows=workflows).workflow_replay_iterator(each()) as results:
        async for result in results:
            if result.replay_failure is None:
                replayed.append(result.history.workflow_id)
            else:
                failed[result.history.workflow_id] = first_line(result.replay_failure)
    return {"replayed": replayed, "failed": failed}


async def running(workflows: Sequence[type] = WORKFLOWS) -> List[WorkflowHistory]:
    """The histories of the running workflows whose types ``workflows`` holds."""
    names = {workflow.__name__ for workflow in workflows}
    client = await connect_temporal()
    histories = []
    async for execution in client.list_workflows(RUNNING):
        if execution.workflow_type in names:
            handle = client.get_workflow_handle(execution.id, run_id=execution.run_id)
            histories.append(await handle.fetch_history())
    return histories


async def run() -> Tuple[Dict[str, Any], int]:
    """The result to print and the exit status."""
    try:
        histories = await running()
    except Exception as exc:
        return {"error": f"listing or fetching the running workflows failed: {first_line(exc)}"}, 2
    return await replay(histories), 0


def main() -> int:
    result, code = asyncio.run(run())
    print(json.dumps(result), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
