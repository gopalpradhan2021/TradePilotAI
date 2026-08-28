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


# --- get_historical_candles (PaperBroker.get_recent_candles() shim during backtest) --

def test_get_historical_candles_returns_only_already_served_bars():
    client = ReplayMarketDataClient({"RELIANCE": _candles(100, 101, 102, 103)})
    client.get_quote("NSE", "CASH", "RELIANCE")
    client.get_quote("NSE", "CASH", "RELIANCE")

    result = client.get_historical_candles(
        exchange="NSE", segment="CASH", groww_symbol="NSE-RELIANCE",
        start_time="2026-01-01 00:00:00", end_time="2026-01-02 00:00:00",
        candle_interval="5minute",
    )

    assert [row[4] for row in result["candles"]] == [100, 101]  # closes, no lookahead


def test_get_historical_candles_before_any_consumption_returns_empty():
    client = ReplayMarketDataClient({"RELIANCE": _candles(100, 101)})

    result = client.get_historical_candles(
        exchange="NSE", segment="CASH", groww_symbol="NSE-RELIANCE",
        start_time="2026-01-01 00:00:00", end_time="2026-01-02 00:00:00",
        candle_interval="5minute",
    )

    assert result == {"candles": []}


def test_get_historical_candles_tolerates_missing_ohlv_fields():
    client = ReplayMarketDataClient({"RELIANCE": _candles(100)})
    client.get_quote("NSE", "CASH", "RELIANCE")

    result = client.get_historical_candles(
        exchange="NSE", segment="CASH", groww_symbol="NSE-RELIANCE",
        start_time="2026-01-01 00:00:00", end_time="2026-01-02 00:00:00",
        candle_interval="5minute",
    )

    row = result["candles"][0]
    assert row[1] == 100  # open falls back to close
    assert row[2] == 100  # high falls back to close
    assert row[3] == 100  # low falls back to close
    assert row[4] == 100  # close
    assert row[5] == 0    # volume falls back to 0
