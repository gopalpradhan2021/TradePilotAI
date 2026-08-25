"""
Best-effort Telegram notifications. Every function here is designed to never
raise — a dead network or bad token must never break the trading loop, only
degrade to a missed notification (logged, not propagated).

Two entrypoints sharing one HTTP call:
  - send_telegram(settings, message): for callers that already have a
    Settings object (main.py, Orchestrator).
  - send_telegram_raw(token, chat_id, message): for callers that only have
    the two config values, not a full Settings (RiskManager, and the
    standalone daily reminder script which deliberately avoids importing
    anything beyond config.settings + this module).
"""
import logging

import requests

from config.settings import Settings

logger = logging.getLogger("groww_agent.notifier")

_HTTP_TIMEOUT_SEC = 5


def _post(token: str, chat_id: str, message: str) -> bool:
    if not token or not chat_id:
        logger.debug("Telegram not configured, skipping notification.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(
            url, json={"chat_id": chat_id, "text": message}, timeout=_HTTP_TIMEOUT_SEC
        )
        if response.status_code != 200:
            logger.error(
                "Telegram send failed: status=%s body=%s", response.status_code, response.text
            )
            return False
        logger.info("Telegram notification sent.")
        return True
    except requests.RequestException as e:
        logger.error("Telegram send failed: %s", e)
        return False
    except Exception as e:
        logger.error("Telegram send failed unexpectedly: %s", e)
        return False


def send_telegram(settings: Settings, message: str) -> bool:
    return _post(settings.telegram_bot_token, settings.telegram_chat_id, message)


def send_telegram_raw(token: str, chat_id: str, message: str) -> bool:
    return _post(token, chat_id, message)
