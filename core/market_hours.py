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


SQUARE_OFF_CUTOFF_IST = dtime(15, 20)


def is_past_square_off_cutoff(now_ist: datetime | None = None) -> bool:
    """True once Groww's real MIS auto-square-off cutoff (3:20 PM IST) has passed on a
    trading day. Deliberately not derived from market_status_ist()'s CLOSING window
    (15:30-16:00) — CLOSING starts 10 minutes too late for this purpose.

    `now_ist` defaults to the real wall-clock IST time — callers that need this to track
    simulated historical time instead (RiskManager, Orchestrator, both during a backtest)
    pass their own injected clock's current instant. Found live 2026-09-01: leaving this
    unparameterized meant every backtest/nightly_optimize run executed after 3:20 PM real
    IST time had every CASH BUY rejected and every open position instantly force-closed,
    regardless of which historical date/time was actually being replayed — a real clock
    leak into supposedly-deterministic simulated time, invalidating that day's results."""
    if now_ist is None:
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    if now_ist.weekday() >= 5:
        return False
    return now_ist.time() >= SQUARE_OFF_CUTOFF_IST
