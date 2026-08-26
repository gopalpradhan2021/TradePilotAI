"""
Standalone daily reminder: Groww's API key+secret flow requires a human to
click "Approve" on Groww's own dashboard every day before the 6 AM IST
reset. There is no API to automate that click, so this just pings ntfy.sh
as a reminder. Deliberately independent of the bot process, the DB, and
Groww auth itself — it must keep working even when the thing it's warning
about (an expired key) is the bot's actual problem. Run via a systemd timer
(see deploy/groww-key-reminder.timer), not imported by anything else.
"""
import logging
import sys

from config.settings import load_settings
from core.notifier import send_notification

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("groww_agent.key_reminder")

MESSAGE = (
    "⏰ Reminder: re-approve the Groww API key before the 6 AM IST reset — "
    "https://groww.in/trade-api/api-keys"
)


def main():
    settings = load_settings()
    sent = send_notification(settings, MESSAGE)
    if not sent:
        logger.warning("Reminder was not sent (unconfigured or send failure) — see log above.")
    sys.exit(0)  # never fail the systemd unit; no further escalation channel exists


if __name__ == "__main__":
    main()
