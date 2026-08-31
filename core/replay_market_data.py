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

    def get_quote(self, exchange, segment, trading_symbol, **kwargs):
        # **kwargs absorbs `timeout` (GROWW_API_TIMEOUT_SEC, now passed on every real
        # PaperBroker.get_ltp() call — see core/execution.py). Found live 2026-08-31: without
        # this, every call TypeErrors on the unexpected kwarg BEFORE this body ever runs —
        # caught by PaperBroker.get_ltp()'s broad except-Exception, so no crash, but the
        # `self._index[trading_symbol] = idx + 1` line below never executes either. That's
        # not just a silently-failed LTP fetch: the replay index never advances, so
        # has_more() stays True forever and run_backtest()'s `while` loop never terminates —
        # a genuine infinite loop, not just a slow one (surfaced as a 3+ hour stall on the
        # live droplet and an unbounded hang in CI before this fix).
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

    def get_historical_candles(self, exchange, segment, groww_symbol, start_time, end_time,
                                candle_interval, **kwargs):
        """Shim for PaperBroker.get_recent_candles() during backtest/replay. Ignores every
        kwarg except which symbol (groww_symbol is "NSE-<symbol>", matching how
        PaperBroker.get_recent_candles() and scripts/backtest.py both construct it) — serves
        only candles ALREADY CONSUMED via get_quote() so far (self._index[symbol] candles),
        i.e. strictly no lookahead into the bar about to be served this cycle or beyond. This
        deliberately mirrors live behavior, where a periodic candle-fetch only ever sees
        already-closed candles while the live LTP ticks inside the next forming bar.

        The trailing **kwargs absorbs `timeout` (GROWW_API_TIMEOUT_SEC, now passed on every
        real call site — see core/execution.py) so this fake doesn't TypeError on it. Found
        live 2026-08-31: without this, every single candle fetch during a backtest/nightly-
        optimize run raised (caught by PaperBroker's broad except-Exception, so it failed
        silently rather than crashing) — turning what should be instant into something with
        real, compounding per-call overhead across every bar of every symbol.

        Returns the same raw {"candles": [[iso_ts, o, h, l, c, v], ...]} shape Groww's real API
        returns, so PaperBroker's own _parse_candles() path is exercised identically in both
        backtest and live/paper. Candle dicts here only guarantee "timestamp"/"close" (see this
        class's own docstring and callers like tests/test_backtest_engine.py's candle
        fixtures) — falls back to `close` for missing open/high/low, 0 for missing volume."""
        symbol = groww_symbol.removeprefix("NSE-")
        candles = self._candles.get(symbol, [])
        idx = self._index.get(symbol, 0)
        served = candles[:idx]
        return {
            "candles": [
                [c["timestamp"].isoformat(), c.get("open", c["close"]), c.get("high", c["close"]),
                 c.get("low", c["close"]), c["close"], c.get("volume", 0)]
                for c in served
            ]
        }
