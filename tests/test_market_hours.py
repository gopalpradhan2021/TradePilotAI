from datetime import datetime

import core.market_hours as market_hours_module
from core.market_hours import is_past_square_off_cutoff


class _FixedDateTime(datetime):
    """Stand-in for datetime.now() that always returns a fixed instant, regardless of
    the tz passed in — lets tests pin core.market_hours' "now" without touching the
    real clock."""
    _fixed: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._fixed.astimezone(tz) if tz else cls._fixed


def _set_now(monkeypatch, year, month, day, hour, minute):
    fixed = datetime(year, month, day, hour, minute, tzinfo=market_hours_module.ZoneInfo("Asia/Kolkata"))
    stub = type("_Fixed", (_FixedDateTime,), {"_fixed": fixed})
    monkeypatch.setattr(market_hours_module, "datetime", stub)


def test_before_cutoff_is_false(monkeypatch):
    _set_now(monkeypatch, 2026, 8, 31, 15, 19)  # Monday
    assert is_past_square_off_cutoff() is False


def test_at_cutoff_is_true(monkeypatch):
    _set_now(monkeypatch, 2026, 8, 31, 15, 20)  # Monday
    assert is_past_square_off_cutoff() is True


def test_after_cutoff_is_true(monkeypatch):
    _set_now(monkeypatch, 2026, 8, 31, 15, 21)  # Monday
    assert is_past_square_off_cutoff() is True


def test_weekend_is_always_false_even_past_cutoff_time(monkeypatch):
    _set_now(monkeypatch, 2026, 9, 5, 16, 0)  # Saturday
    assert is_past_square_off_cutoff() is False
