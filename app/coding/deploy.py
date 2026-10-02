"""What deploying a change to Aura's own code touches, read from the paths it changes."""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable, List

# (path or directory prefix, services restarted when it changes); docs, tests and ops restart nothing.
SERVICE_PATHS = (
    ("app/", ("rmp-api", "rmp-worker")),
    ("worker.py", ("rmp-api", "rmp-worker")),
    ("requirements.txt", ("rmp-api", "rmp-worker")),
    ("plugins/", ("openclaw-gateway",)),
    ("web-stack/", ("aura-web-backends",)),
)


def restarts_for(paths: Iterable[str]) -> List[str]:
    """The services a deploy restarts: the API and worker for app code, a changed unit under systemd/ itself."""
    services = set()
    for path in paths:
        if path.startswith("systemd/") and path.endswith((".service", ".timer")):
            name = PurePosixPath(path).name
            services.add(name.removesuffix(".service"))
        for prefix, units in SERVICE_PATHS:
            if path == prefix or (prefix.endswith("/") and path.startswith(prefix)):
                services.update(units)
    return sorted(services)
