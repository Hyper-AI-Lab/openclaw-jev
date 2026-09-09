"""Slack conversation identity vs OpenClaw main session."""
from __future__ import annotations

from typing import Iterable, List, Optional

MAIN_SESSION = "agent:main:main"


def is_slack_conversation_key(session_key: str) -> bool:
    key = (session_key or "").lower()
    return "slack:" in key


def is_main_session_key(session_key: str) -> bool:
    key = (session_key or "").strip()
    return key == MAIN_SESSION or (
        key.endswith(":main") and not is_slack_conversation_key(key)
    )


def canonical_user_session_key(session_key: str) -> str:
    """Keep Slack conversation keys; strip only whitespace."""
    return (session_key or "").strip()


def pick_slack_session_key(candidates: Iterable[Optional[str]]) -> Optional[str]:
    """First real Slack conversation key; prefer DM channel (``slack:channel:d…``)."""
    slack = [
        canonical_user_session_key(c or "")
        for c in candidates
        if is_slack_conversation_key(c or "")
    ]
    if not slack:
        return None
    slack.sort(
        key=lambda k: (0 if ":slack:channel:d" in k.lower() else 1, k.lower())
    )
    return slack[0]


def discover_slack_conversation_key(
    *,
    store_keys: Optional[Iterable[str]] = None,
    owner_uid: str = "",
) -> Optional[str]:
    """Pick the host Slack DM conversation key from the OpenClaw session store."""
    keys: List[str]
    if store_keys is not None:
        keys = [str(k) for k in store_keys]
    else:
        try:
            from app.openclaw_sessions import iter_session_entries

            keys = [k for k, _ in iter_session_entries()]
        except Exception:
            return None
    slack = [k for k in keys if is_slack_conversation_key(k)]
    if not slack:
        return None
    owner = (owner_uid or "").lower()
    if not owner:
        try:
            from app.config import get_slack_owner_user_id

            owner = (get_slack_owner_user_id() or "").lower()
        except Exception:
            owner = ""

    def sort_key(k: str) -> tuple:
        kl = k.lower()
        dm = 0 if ":slack:channel:d" in kl else 1
        owner_hit = 0 if owner and owner in kl else 1
        return (dm, owner_hit, kl)

    slack.sort(key=sort_key)
    return slack[0]


def persist_user_session_key(
    reported: str,
    *,
    task_type: str = "user",
    store_keys: Optional[Iterable[str]] = None,
    discover: bool = True,
) -> str:
    """User DMs store the Slack conversation key; cron/canary keep main."""
    reported = canonical_user_session_key(reported) or MAIN_SESSION
    if task_type != "user":
        return reported
    picked = pick_slack_session_key([reported])
    if picked:
        return picked
    if not discover:
        return reported
    return discover_slack_conversation_key(store_keys=store_keys) or reported


def dialogue_lookup_keys(session_key: str) -> List[str]:
    """Keys to scan for RECENT DIALOGUE.

    New user DMs persist the real Slack session. Older rows were stored on
    ``agent:main:main``; include that as backfill when the inbound key is Slack.
    """
    key = canonical_user_session_key(session_key)
    if not key:
        return []
    keys = [key]
    if is_slack_conversation_key(key) and MAIN_SESSION not in keys:
        keys.append(MAIN_SESSION)
    return keys


def active_task_lookup_keys(
    session_key: str,
    *,
    store_keys: Optional[Iterable[str]] = None,
    discover: bool = True,
) -> List[str]:
    """Session keys that still refer to the same user Slack conversation."""
    key = canonical_user_session_key(session_key)
    keys: List[str] = [key] if key else []
    if is_slack_conversation_key(key):
        if MAIN_SESSION not in keys:
            keys.append(MAIN_SESSION)
        return keys
    if key == MAIN_SESSION and discover:
        discovered = discover_slack_conversation_key(store_keys=store_keys)
        if discovered and discovered not in keys:
            keys.append(discovered)
    return keys


def session_keys_equivalent(incoming: str, stored: str) -> bool:
    """Same Slack conversation, including legacy rows keyed on main.

    Incoming Slack matches stored main (transition). Incoming main does **not**
    match stored Slack — canaries stay on main and must not attach to user DMs.
    """
    a = canonical_user_session_key(incoming)
    b = canonical_user_session_key(stored)
    if not a:
        return True
    if a == b:
        return True
    if is_slack_conversation_key(a) and b == MAIN_SESSION:
        return True
    return False
