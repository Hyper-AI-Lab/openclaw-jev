"""Whole-message stop/cancel — not incidental mentions."""
from __future__ import annotations

import re

_WHOLE_STOP = re.compile(
    r"^(?:[.!?,:;]+\s*)?(?:please\s+)?(stop|abort|cancel|halt)(?:[.!?]*)?$",
    re.IGNORECASE,
)


def is_whole_message_stop(text: str) -> bool:
    """True only when the entire message is stop/cancel/abort/halt.

    Optional leading punctuation and ``please stop`` match. ``don't stop the
    canary yet`` does not.
    """
    return bool(_WHOLE_STOP.match((text or "").strip()))
