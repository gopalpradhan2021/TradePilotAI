"""
Standalone operator kill-switch. Run via: python -m scripts.halt_bot ["reason"]

Independent of the running bot process — halts by writing directly to the
shared SQLite daily_summary row, which the running bot's Orchestrator polls
every cycle (see RiskManager.refresh_halt_state()), so it takes effect on
the bot's next poll (within poll_interval_sec), no restart needed.

Unlike scripts/groww_key_reminder.py, this does NOT swallow failures — it's
an interactive SSH tool, the operator needs immediate pass/fail feedback.
"""
import logging
import sys

from config.settings import load_settings
from core.risk_manager import RiskManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("groww_agent.halt_bot")


def main():
    reason = " ".join(sys.argv[1:]) or "manual kill switch"
    settings = load_settings()
    rm = RiskManager(settings.risk, ntfy_topic=settings.ntfy_topic)
    rm.manual_halt(reason)
    print(f"HALTED. reason={reason!r} halt_source=MANUAL trade_date={rm._current_day.isoformat()}")


if __name__ == "__main__":
    main()
