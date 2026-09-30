"""The Claude subscription token for coding runs: captured once, stored for systemd only.

``claude setup-token`` prints a one-year OAuth token that the terminal may wrap across lines
(claude-code#54738). It is stored as ``CLAUDE_CODE_OAUTH_TOKEN`` in a root-only environment file
that systemd hands to each coding unit, plus metadata (issue date, expiry, a short fingerprint)
that readiness can report without reading the secret.

    venv/bin/python -m app.coding.credentials extract <transcript>   # prints the token
    venv/bin/python -m app.coding.credentials store < token          # token on stdin
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

TOKEN_PREFIX = "sk-ant-oat01-"
TOKEN_LIFETIME_DAYS = 365
# Complete tokens are about 108 characters; a shorter match ending its line was wrapped.
_COMPLETE_MIN = 100
_TOKEN_CHARS = r"[A-Za-z0-9_-]"
_TOKEN = re.compile(rf"{re.escape(TOKEN_PREFIX)}{_TOKEN_CHARS}+")
_CONTINUATION = re.compile(rf"^\s*({_TOKEN_CHARS}+)\s*$")
_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]")
_VALID = re.compile(rf"^{re.escape(TOKEN_PREFIX)}{_TOKEN_CHARS}{{60,}}$")


def extract_token(transcript: str) -> Optional[str]:
    """The token in a terminal transcript, rejoined when the terminal wrapped it."""
    text = _ESCAPES.sub("", transcript).replace("\r", "")
    lines = text.split("\n")
    for index, line in enumerate(lines):
        m = _TOKEN.search(line)
        if not m:
            continue
        token = m.group(0)
        if len(token) < _COMPLETE_MIN and m.end() == len(line.rstrip()) and index + 1 < len(lines):
            nxt = _CONTINUATION.match(lines[index + 1])
            if nxt:
                token += nxt.group(1)
        return token
    return None


def is_valid(token: str) -> bool:
    return bool(_VALID.match(token or ""))


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def write_token(token: str, env_file: Path, meta_file: Path, *, now: Optional[datetime] = None) -> dict:
    """Write the environment file (0600) and its metadata atomically; returns the metadata."""
    if not is_valid(token):
        raise ValueError(f"not a Claude Code token ({TOKEN_PREFIX}…)")
    issued = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    meta = {
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(days=TOKEN_LIFETIME_DAYS)).isoformat(),
        "length": len(token),
        "fingerprint": fingerprint(token),
    }
    env_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_write(env_file, f"CLAUDE_CODE_OAUTH_TOKEN={token}\n")
    _atomic_write(meta_file, json.dumps(meta, indent=2) + "\n")
    return meta


def read_meta(meta_file: Path) -> Optional[dict]:
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def main(argv: Sequence[str], *, env_file: Optional[Path] = None, meta_file: Optional[Path] = None) -> int:
    from app.coding.units import TOKEN_ENV_FILE, TOKEN_META_FILE

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    extract = sub.add_parser("extract", help="print the token found in a setup-token transcript")
    extract.add_argument("transcript")
    sub.add_parser("store", help="store the token read from stdin")
    args = parser.parse_args(argv)
    if args.action == "extract":
        token = extract_token(Path(args.transcript).read_text(encoding="utf-8", errors="replace"))
        if not token:
            print("[claude-login] no token found in the setup-token output; rerun with --paste", file=sys.stderr)
            return 1
        sys.stdout.write(token)
        return 0
    token = "".join(sys.stdin.read().split())
    try:
        meta = write_token(token, env_file or TOKEN_ENV_FILE, meta_file or TOKEN_META_FILE)
    except ValueError as exc:
        print(f"[claude-login] {exc} (got {len(token)} characters)", file=sys.stderr)
        return 1
    print(f"[claude-login] token stored ({meta['length']} chars, fingerprint {meta['fingerprint']}, "
          f"expires {meta['expires_at'][:10]})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
