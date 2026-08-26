"""
NSE session-time helper, shared by dashboard/app.py (display) and
scripts/nightly_optimize.py (a defensive check that it's actually running after-hours before
doing any work, even though the systemd timer schedule already ensures that).
"""
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo


def market_status_ist() -> tuple[str, str, datetime]:
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    weekday = now_ist.weekday()
    t = now_ist.time()
    if weekday >= 5:
        return "CLOSED", "Weekend", now_ist
    if dtime(9, 0) <= t < dtime(9, 15):
        return "PRE-OPEN", "Pre-open session", now_ist
    if dtime(9, 15) <= t < dtime(15, 30):
        return "OPEN", "Regular trading session", now_ist
    if dtime(15, 30) <= t < dtime(16, 0):
        return "CLOSING", "Closing/post-close session", now_ist
    return "CLOSED", "Outside trading hours", now_ist


def is_market_open() -> bool:
    return market_status_ist()[0] == "OPEN"
