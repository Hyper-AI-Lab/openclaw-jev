"""A revision's kind, read from its file's one line ``KIND = "schema"`` or ``KIND = "data"`` (specs/WP-26.md WP26-05).

A data revision changes rows the old code cannot put back, so writing one needs a recent backup. app/db/migrate.py
reads each pending revision's file through ``kind_of``, and so does the deploy's check before its lock, so the two
never disagree about a revision written ``KIND = 'data'``, ``KIND: str = "data"`` or indented: each of those is no
kind. The module's ``KIND`` attribute is never read. Standard library only.
"""
import re
from typing import Optional

KIND_LINE = re.compile(r'^KIND = "(schema|data)"$', re.MULTILINE)


def kind_of(text: str) -> Optional[str]:
    """The kind when exactly one line of ``text`` is a KIND line, else None."""
    found = KIND_LINE.findall(text)
    return found[0] if len(found) == 1 else None
