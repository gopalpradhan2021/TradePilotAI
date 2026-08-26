"""
Feeds pre-fetched historical candles through the same get_quote() shape
PaperBroker expects from a live Groww client. This is what lets
scripts/backtest.py drive the real, unmodified Orchestrator -> RiskManager ->
PaperBroker pipeline with historical data instead of live prices: "backtest"
and "market replay" are the same run, just against recorded candles.
"""
import logging
from datetime import datetime

logger = logging.getLogger("groww_agent.replay")


class ReplayExhausted(Exception):
    pass


class ReplayMarketDataClient:
    # Only the constants PaperBroker's _groww_segment()/get_ltp() touch.
    EXCHANGE_NSE = "NSE"
    SEGMENT_CASH = "CASH"
    SEGMENT_FNO = "FNO"

    def __init__(self, candles_by_symbol: dict[str, list[dict]]):
        """candles_by_symbol: {symbol: [{"timestamp": datetime, "close": float, ...}, ...]},
        each list sorted ascending by timestamp."""
        self._candles = candles_by_symbol
        self._index = {symbol: 0 for symbol in candles_by_symbol}

    def get_quote(self, exchange, segment, trading_symbol):
        candles = self._candles.get(trading_symbol, [])
        idx = self._index.get(trading_symbol, 0)
        if idx >= len(candles):
            raise ReplayExhausted(f"No more replay candles for {trading_symbol}")
        candle = candles[idx]
        self._index[trading_symbol] = idx + 1
        return {"last_price": candle["close"], "ltp": candle["close"]}

    def has_more(self, symbol: str) -> bool:
        return self._index.get(symbol, 0) < len(self._candles.get(symbol, []))

    def peek_next_timestamp(self, symbol: str) -> datetime | None:
        """Timestamp of the next candle that will be served for `symbol`, without
        consuming it — used to advance the simulated clock before each cycle."""
        candles = self._candles.get(symbol, [])
        idx = self._index.get(symbol, 0)
        if idx >= len(candles):
            return None
        return candles[idx]["timestamp"]

    def bars_consumed(self, symbol: str) -> int:
        return self._index.get(symbol, 0)
