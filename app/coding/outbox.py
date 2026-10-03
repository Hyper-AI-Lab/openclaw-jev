"""Files Aura sends Kirill with her reply: made in one of the task's Claude sessions.

She attaches a file while she works (``POST /api/replies/files``); RMP records its checksum, and uploads it into
Kirill's DM after the reply the evaluator accepted (``send_reply_files``). The file is checked again right before
the upload, so what goes out is what was attached.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict

from app.coding import direct
from app.memory.policy import redact_secrets

MAX_BYTES = 50 * 1024 * 1024
MAX_FILES = 10


class FileRefused(Exception):
    """A file RMP won't send: outside the task's Claude sessions, not a regular file, too big, or holding a secret."""


def check(task_id: str, path: str) -> Dict[str, Any]:
    """The file's name, real path, size and SHA-256, if RMP may send it for the task."""
    try:
        real = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise FileRefused(f"{path} does not exist")
    if (direct.DIRECT_DIR / task_id).resolve() not in real.parents:
        raise FileRefused("only a file from one of this task's Claude sessions can be sent")
    if not real.is_file():
        raise FileRefused(f"{real.name} is not a regular file")
    size = real.stat().st_size
    if not 0 < size <= MAX_BYTES:
        raise FileRefused(f"{real.name} is {size} bytes; a file can have 1 to {MAX_BYTES} bytes")
    data = real.read_bytes()
    if _holds_secret(data):
        raise FileRefused(f"{real.name} looks like it holds a secret, so it is not sent")
    return {"name": real.name, "path": str(real), "size": size, "sha256": hashlib.sha256(data).hexdigest()}


def _holds_secret(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return redact_secrets(text) != text
