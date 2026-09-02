"""
Moving Average Crossover + RSI filter strategy.

Entry (BUY): short MA crosses above long MA, AND RSI is in a healthy
             range (not already overbought).
Exit (SELL): short MA crosses back below long MA, OR RSI is overbought,
             OR a stop-loss threshold from entry price is hit.

This strategy maintains its own rolling candle-close history per symbol,
populated by update_candles() from real OHLC candles (fetched periodically by
Orchestrator via broker.get_recent_candles(), on a coarser cadence than the
per-cycle poll) rather than from raw per-poll tick prices — see the
CANDLE_INTERVAL comment below for why. decide() is still called every cycle
with the latest last_traded_price, which drives execution (fill/reference
price) and the stop-loss check, independent of the candle-close series used
for MA/RSI.
"""
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from core.models import ProposedOrder, Side, OrderType
from strategies.base_strategy import BaseStrategy

logger = logging.getLogger("groww_agent.strategy.ma_rsi")

# LONG_WINDOW/RSI_ENTRY_MIN/MIN_CROSSOVER_GAP_PCT/COOLDOWN_SECONDS set to the winning
# combo from a 5-minute intraday nightly_optimize sweep (2026-09-02, 243-combo grid,
# 28-day lookback) — the only one of 15 watched symbols where any candidate beat the
# prior defaults out-of-sample with real confidence (RELIANCE: +82.55 vs -181.53).
# Applies to every watched symbol (no per-symbol params exist); not validated against
# the other 14 symbols' own data specifically.
SHORT_WINDOW = 9
LONG_WINDOW = 15
RSI_WINDOW = 14
# Entry floor lowered from 40 (2026-08-28) to admit trades recovering from a
# dip rather than only ones already mid-uptrend — ceiling stays at 70, well
# clear of RSI_EXIT_OVERBOUGHT (75), so an entry is never one tick from its
# own exit trigger.
RSI_ENTRY_MIN = 35
RSI_ENTRY_MAX = 70
RSI_EXIT_OVERBOUGHT = 75
STOP_LOSS_PCT = 2.0
# Nominal placeholder — this strategy stays capital-agnostic (CLAUDE.md), so it can't
# compute a real position size itself. Orchestrator._handle_proposed_order() overwrites
# order.qty for every CASH order before it's ever persisted: capital-aware sizing on
# entry (core/position_sizing.py), the real held qty on exit.
DEFAULT_ORDER_QTY = 1

# Minimum relative gap between short/long MA required to count as a real
# crossover, not sub-tick price noise. Noise floor observed live on
# 2026-08-26 was ~0.0008% (RELIANCE oscillating 1304-1307). Lowered from
# 0.0005 (~60x that floor) to 0.0002 (2026-08-28) to catch more real
# crossovers after a quiet first two live days produced almost no trades;
# raised to 0.0003 (2026-09-02) — see the nightly_optimize note above.
MIN_CROSSOVER_GAP_PCT = 0.0003

# Minimum wall-clock time after closing a position before a new entry is
# allowed — kills the sub-minute flip-flops seen live (5s, 20s holds).
COOLDOWN_SECONDS = 30

# MA/RSI are computed off real OHLC candle closes (fetched periodically by
# Orchestrator via broker.get_recent_candles(), see update_candles() below),
# not raw per-poll tick prices — added 2026-08-28 after a live incident where
# a frozen-then-resumed price pinned RSI near 100 within seconds on tick data
# (RSI_WINDOW=14 at a 5s poll is only 70 seconds of history, far too
# short-horizon and noise-prone). CANDLE_LOOKBACK_BARS gives headroom over the
# warmup floor (max(SHORT_WINDOW, LONG_WINDOW, RSI_WINDOW+1) = 22) and over
# nightly_optimize.py's parameter-sweep grid maximum.
CANDLE_INTERVAL = "5minute"
CANDLE_LOOKBACK_BARS = 60


@dataclass(frozen=True)
class MARsiParams:
    """Tunable strategy constants, defaulting to the production values above.
    scripts/nightly_optimize.py sweeps these; live/paper trading (main.py) and
    scripts/backtest.py both use the defaults unless told otherwise."""
    short_window: int = SHORT_WINDOW
    long_window: int = LONG_WINDOW
    rsi_window: int = RSI_WINDOW
    rsi_entry_min: float = RSI_ENTRY_MIN
    rsi_entry_max: float = RSI_ENTRY_MAX
    rsi_exit_overbought: float = RSI_EXIT_OVERBOUGHT
    stop_loss_pct: float = STOP_LOSS_PCT
    min_crossover_gap_pct: float = MIN_CROSSOVER_GAP_PCT
    cooldown_seconds: float = COOLDOWN_SECONDS
    candle_interval: str = CANDLE_INTERVAL
    candle_lookback_bars: int = CANDLE_LOOKBACK_BARS


def _sma(prices: list[float], window: int) -> float | None:
    if len(prices) < window:
        return None
    return sum(prices[-window:]) / window


def _rsi(prices: list[float], window: int) -> float | None:
    if len(prices) < window + 1:
        return None
    gains, losses = [], []
    for i in range(-window, 0):
        change = prices[i] - prices[i - 1]
        if change >= 0:
            gains.append(change)
        else:
            losses.append(-change)
    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


class SymbolState:
    def __init__(self, max_len: int = 200):
        self.prices: deque[float] = deque(maxlen=max_len)
        self.in_position = False
        self.entry_price: float | None = None
        self.prev_short_ma: float | None = None
        self.prev_long_ma: float | None = None
        self.last_exit_time: float | None = None


class MARsiStrategy(BaseStrategy):
    def __init__(self, clock: Callable[[], float] | None = None,
                 params: MARsiParams | None = None):
        """clock defaults to the real monotonic clock (resolved at construction time, not
        at class-definition time, so tests can monkeypatch time.monotonic beforehand);
        scripts/backtest.py injects a simulated one derived from each historical bar's
        timestamp, so COOLDOWN_SECONDS reflects simulated bar-to-bar time rather than the
        backtest process's real (much faster) execution speed.

        params defaults to the production constants (MARsiParams()); scripts/nightly_optimize.py
        injects candidate parameter sets to backtest without touching the module constants."""
        self._state: dict[str, SymbolState] = {}
        self._clock = clock if clock is not None else time.monotonic
        self.params = params if params is not None else MARsiParams()

    def _get_state(self, symbol: str) -> SymbolState:
        if symbol not in self._state:
            self._state[symbol] = SymbolState()
        return self._state[symbol]

    def restore_position(self, symbol: str, entry_price: float) -> None:
        state = self._get_state(symbol)
        state.in_position = True
        state.entry_price = entry_price
        logger.info("%s: restored open position on startup, entry_price=%.2f", symbol, entry_price)

    def force_exit(self, symbol: str) -> None:
        # Mirrors decide()'s own SELL branch exactly — deliberately leaves
        # prev_short_ma/prev_long_ma untouched (crossover-detection history is independent
        # of position state).
        state = self._get_state(symbol)
        state.in_position = False
        state.entry_price = None
        state.last_exit_time = self._clock()

    def get_candle_requirements(self) -> tuple[str, int]:
        return self.params.candle_interval, self.params.candle_lookback_bars

    def update_candles(self, symbol: str, candles: list[dict]) -> None:
        """Replaces (not appends) this symbol's candle-close series — called by Orchestrator
        on its coarser candle-fetch cadence, not every decide() cycle. An empty `candles` list
        is a no-op (keeps the existing series), which is what makes a repeated fetch of an
        unchanged (still-forming) candle set genuinely idempotent rather than merely
        coincidentally so."""
        if not candles:
            return
        state = self._get_state(symbol)
        closes = [c["close"] for c in candles]
        state.prices = deque(closes[-state.prices.maxlen:], maxlen=state.prices.maxlen)

    def get_debug_info(self, symbol: str) -> dict:
        p = self.params
        state = self._get_state(symbol)
        prices = list(state.prices)

        short_ma = _sma(prices, p.short_window)
        long_ma = _sma(prices, p.long_window)
        rsi = _rsi(prices, p.rsi_window)
        gap_pct = abs(short_ma - long_ma) / long_ma * 100 if short_ma is not None and long_ma else None

        cooldown_remaining_sec = None
        if not state.in_position and state.last_exit_time is not None:
            remaining = p.cooldown_seconds - (self._clock() - state.last_exit_time)
            cooldown_remaining_sec = round(max(0.0, remaining), 1)

        return {
            "prices_collected": len(prices),
            "prices_needed": max(p.short_window, p.long_window, p.rsi_window + 1),
            "warmed_up": short_ma is not None and long_ma is not None and rsi is not None,
            "short_ma": round(short_ma, 2) if short_ma is not None else None,
            "long_ma": round(long_ma, 2) if long_ma is not None else None,
            "gap_pct": round(gap_pct, 4) if gap_pct is not None else None,
            "min_gap_pct": round(p.min_crossover_gap_pct * 100, 4),
            "rsi": round(rsi, 1) if rsi is not None else None,
            "rsi_entry_band": [p.rsi_entry_min, p.rsi_entry_max],
            "rsi_exit_overbought": p.rsi_exit_overbought,
            "in_position": state.in_position,
            "entry_price": state.entry_price,
            "cooldown_remaining_sec": cooldown_remaining_sec,
        }

    def decide(self, symbol: str, last_traded_price: float | None) -> ProposedOrder | None:
        if last_traded_price is None:
            return None

        p = self.params
        state = self._get_state(symbol)
        # state.prices is populated out-of-band by update_candles() from real candle closes,
        # not appended here from last_traded_price — see CANDLE_INTERVAL comment above.
        # last_traded_price is still used below for the stop-loss check and as the fill
        # reference, so execution stays on live tick data even though signals don't.
        prices = list(state.prices)

        short_ma = _sma(prices, p.short_window)
        long_ma = _sma(prices, p.long_window)
        rsi = _rsi(prices, p.rsi_window)

        if short_ma is None or long_ma is None or rsi is None:
            logger.info(
                "%s: warming up (%d/%d prices collected)",
                symbol, len(prices), max(p.short_window, p.long_window, p.rsi_window + 1),
            )
            state.prev_short_ma, state.prev_long_ma = short_ma, long_ma
            return None

        gap_pct = abs(short_ma - long_ma) / long_ma

        crossed_up = (
            state.prev_short_ma is not None and state.prev_long_ma is not None
            and state.prev_short_ma <= state.prev_long_ma
            and short_ma > long_ma
            and gap_pct >= p.min_crossover_gap_pct
        )
        crossed_down = (
            state.prev_short_ma is not None and state.prev_long_ma is not None
            and state.prev_short_ma >= state.prev_long_ma
            and short_ma < long_ma
            and gap_pct >= p.min_crossover_gap_pct
        )

        order = None

        if not state.in_position:
            cooldown_active = (
                state.last_exit_time is not None
                and (self._clock() - state.last_exit_time) < p.cooldown_seconds
            )
            if crossed_up and not cooldown_active and p.rsi_entry_min <= rsi <= p.rsi_entry_max:
                order = ProposedOrder(
                    symbol=symbol,
                    side=Side.BUY,
                    qty=DEFAULT_ORDER_QTY,
                    order_type=OrderType.MARKET,
                    reason=(
                        f"MA crossover UP (short={short_ma:.2f} > long={long_ma:.2f}), "
                        f"RSI={rsi:.1f} in healthy range"
                    ),
                )
                state.in_position = True
                state.entry_price = last_traded_price
                logger.info("%s: BUY signal — %s", symbol, order.reason)
        else:
            stop_hit = (
                state.entry_price is not None
                and last_traded_price <= state.entry_price * (1 - p.stop_loss_pct / 100)
            )
            if crossed_down:
                order = ProposedOrder(
                    symbol=symbol,
                    side=Side.SELL,
                    qty=DEFAULT_ORDER_QTY,
                    order_type=OrderType.MARKET,
                    reason=f"MA crossover DOWN (short={short_ma:.2f} < long={long_ma:.2f})",
                )
            elif rsi >= p.rsi_exit_overbought:
                order = ProposedOrder(
                    symbol=symbol,
                    side=Side.SELL,
                    qty=DEFAULT_ORDER_QTY,
                    order_type=OrderType.MARKET,
                    reason=f"RSI overbought ({rsi:.1f} >= {p.rsi_exit_overbought}) — taking profit",
                )
            elif stop_hit:
                order = ProposedOrder(
                    symbol=symbol,
                    side=Side.SELL,
                    qty=DEFAULT_ORDER_QTY,
                    order_type=OrderType.MARKET,
                    reason=(
                        f"Stop-loss hit: price {last_traded_price:.2f} <= "
                        f"entry {state.entry_price:.2f} * {1 - p.stop_loss_pct/100:.3f}"
                    ),
                )

            if order is not None:
                logger.info("%s: SELL signal — %s", symbol, order.reason)
                state.in_position = False
                state.entry_price = None
                state.last_exit_time = self._clock()

        state.prev_short_ma, state.prev_long_ma = short_ma, long_ma
        return order
