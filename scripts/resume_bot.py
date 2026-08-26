"""
Standalone operator resume: clears a MANUALLY-triggered halt only.
Run via: python -m scripts.resume_bot ["reason"]

Refuses (raises, non-zero exit) to clear an automatic daily-loss halt —
that must wait for the next trading day by design, so this can't be used
to accidentally bypass real risk management.
"""
import sys

from config.settings import load_settings
from core.risk_manager import RiskManager


def main():
    reason = " ".join(sys.argv[1:]) or "manual resume"
    settings = load_settings()
    rm = RiskManager(settings.risk, ntfy_topic=settings.ntfy_topic)
    if not rm.halted:
        print(f"Not currently halted — nothing to resume. trade_date={rm._current_day.isoformat()}")
        return
    rm.resume(reason)
    print(f"RESUMED. reason={reason!r} trade_date={rm._current_day.isoformat()}")


if __name__ == "__main__":
    main()
