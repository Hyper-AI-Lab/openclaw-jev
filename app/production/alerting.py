"""Production alerting via configurable webhook."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

import httpx

from app.config import load_settings

logger = logging.getLogger("rmp.alerting")


def get_alerting_config() -> dict:
    settings = load_settings()
    return settings.get("production", {}).get("alerting", {})


def is_alerting_enabled() -> bool:
    cfg = get_alerting_config()
    return bool(cfg.get("enabled") and cfg.get("webhook_url"))


async def send_alert(
    event_type: str,
    message: str,
    *,
    severity: str = "warning",
    context: Optional[Dict[str, Any]] = None,
) -> bool:
    cfg = get_alerting_config()
    if not cfg.get("enabled"):
        return False
    url = cfg.get("webhook_url", "")
    if not url:
        return False

    payload = {
        "source": "rmp",
        "event_type": event_type,
        "severity": severity,
        "message": message,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "context": context or {},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                logger.warning("Alert webhook returned %s: %s", r.status_code, r.text[:200])
                return False
            return True
    except Exception as e:
        logger.exception("Failed to send alert: %s", e)
        return False
