from datetime import date

from core.fno_market_data import FnoMarketData
from core.models import OptionChainSnapshot


class StubBroker:
    def __init__(self, expiries=None, chain=None):
        self._expiries = expiries
        self._chain = chain
        self.chain_calls = []

    def get_expiries(self, underlying):
        return self._expiries

    def get_option_chain(self, underlying, expiry_date):
        self.chain_calls.append((underlying, expiry_date))
        return self._chain


def make_snapshot(expiry):
    return OptionChainSnapshot(
        underlying="NIFTY", underlying_ltp=24000.0, expiry_date=expiry,
        fetched_at=None, strikes=[],
    )


def test_picks_nearest_upcoming_expiry():
    broker = StubBroker(
        expiries=[date(2026, 1, 6), date(2026, 9, 1), date(2026, 9, 8)],
        chain=make_snapshot(date(2026, 9, 1)),
    )
    md = FnoMarketData(broker, today_fn=lambda: date(2026, 8, 28))

    result = md.get_chain("NIFTY")

    assert result is not None
    assert broker.chain_calls == [("NIFTY", date(2026, 9, 1))]


def test_filters_out_past_expiries():
    broker = StubBroker(expiries=[date(2026, 1, 6), date(2026, 1, 13)])
    md = FnoMarketData(broker, today_fn=lambda: date(2026, 8, 28))

    result = md.get_chain("NIFTY")

    assert result is None
    assert broker.chain_calls == []


def test_returns_none_when_no_expiries():
    broker = StubBroker(expiries=None)
    md = FnoMarketData(broker, today_fn=lambda: date(2026, 8, 28))
    assert md.get_chain("NIFTY") is None


def test_returns_none_when_expiries_empty_list():
    broker = StubBroker(expiries=[])
    md = FnoMarketData(broker, today_fn=lambda: date(2026, 8, 28))
    assert md.get_chain("NIFTY") is None


def test_expiry_equal_to_today_counts_as_upcoming():
    broker = StubBroker(expiries=[date(2026, 8, 28)], chain=make_snapshot(date(2026, 8, 28)))
    md = FnoMarketData(broker, today_fn=lambda: date(2026, 8, 28))

    result = md.get_chain("NIFTY")

    assert result is not None
    assert broker.chain_calls == [("NIFTY", date(2026, 8, 28))]
