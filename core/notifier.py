"""
Best-effort push notifications via ntfy.sh (https://ntfy.sh) — a free,
no-signup push service where a topic name doubles as the "password"
(publish/subscribe to https://ntfy.sh/<topic>). Chosen over Telegram after
the user's Telegram account was blocked from creating new bots by
Telegram itself; this has no such account-level gate.

Every function here is designed to never raise — a dead network or an
unconfigured topic must never break the trading loop, only degrade to a
missed notification (logged, not propagated).

Two entrypoints sharing one HTTP call:
  - send_notification(settings, message): for callers that already have a
    Settings object (main.py, Orchestrator).
  - send_notification_raw(topic, message): for callers that only have the
    raw topic string, not a full Settings (RiskManager, and the standalone
    daily reminder script which deliberately avoids importing anything
    beyond config.settings + this module).
"""
import logging

import requests

from config.settings import Settings

logger = logging.getLogger("groww_agent.notifier")

_HTTP_TIMEOUT_SEC = 5
_NTFY_BASE_URL = "https://ntfy.sh"


def _post(topic: str, message: str, title: str = "") -> bool:
    if not topic:
        logger.debug("ntfy topic not configured, skipping notification.")
        return False

    url = f"{_NTFY_BASE_URL}/{topic}"
    headers = {"Title": title} if title else {}
    try:
        response = requests.post(
            url, data=message.encode("utf-8"), headers=headers, timeout=_HTTP_TIMEOUT_SEC
        )
        if response.status_code != 200:
            logger.error(
                "ntfy send failed: status=%s body=%s", response.status_code, response.text
            )
            return False
        logger.info("ntfy notification sent.")
        return True
    except requests.RequestException as e:
        logger.error("ntfy send failed: %s", e)
        return False
    except Exception as e:
        logger.error("ntfy send failed unexpectedly: %s", e)
        return False


def send_notification(settings: Settings, message: str, title: str = "") -> bool:
    return _post(settings.ntfy_topic, message, title)


def send_notification_raw(topic: str, message: str, title: str = "") -> bool:
    return _post(topic, message, title)
