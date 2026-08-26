from datetime import datetime, timedelta

import pytest

from core.replay_market_data import ReplayExhausted, ReplayMarketDataClient


def _candles(*closes, start=datetime(2026, 1, 1, 9, 15)):
    return [{"timestamp": start + timedelta(minutes=5 * i), "close": c} for i, c in enumerate(closes)]


def test_get_quote_returns_successive_closes():
    client = ReplayMarketDataClient({"RELIANCE": _candles(100, 101, 102)})

    q1 = client.get_quote("NSE", "CASH", "RELIANCE")
    q2 = client.get_quote("NSE", "CASH", "RELIANCE")

    assert q1["last_price"] == 100
    assert q2["last_price"] == 101


def test_has_more_false_once_exhausted():
    client = ReplayMarketDataClient({"RELIANCE": _candles(100, 101)})
    client.get_quote("NSE", "CASH", "RELIANCE")
    client.get_quote("NSE", "CASH", "RELIANCE")

    assert client.has_more("RELIANCE") is False


def test_get_quote_raises_once_exhausted():
    client = ReplayMarketDataClient({"RELIANCE": _candles(100)})
    client.get_quote("NSE", "CASH", "RELIANCE")

    with pytest.raises(ReplayExhausted):
        client.get_quote("NSE", "CASH", "RELIANCE")


def test_peek_next_timestamp_does_not_consume():
    candles = _candles(100, 101)
    client = ReplayMarketDataClient({"RELIANCE": candles})

    ts = client.peek_next_timestamp("RELIANCE")

    assert ts == candles[0]["timestamp"]
    assert client.bars_consumed("RELIANCE") == 0


def test_peek_next_timestamp_none_when_exhausted():
    client = ReplayMarketDataClient({"RELIANCE": _candles(100)})
    client.get_quote("NSE", "CASH", "RELIANCE")

    assert client.peek_next_timestamp("RELIANCE") is None


def test_unknown_symbol_has_no_candles():
    client = ReplayMarketDataClient({"RELIANCE": _candles(100)})

    assert client.has_more("TCS") is False
