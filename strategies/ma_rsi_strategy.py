"""
Moving Average Crossover + RSI filter strategy.

Entry (BUY): short MA crosses above long MA, AND RSI is in a healthy
             range (not already overbought).
Exit (SELL): short MA crosses back below long MA, OR RSI is overbought,
             OR a stop-loss threshold from entry price is hit.

This strategy maintains its own rolling price history per symbol (fed by
whatever last_traded_price the orchestrator passes in each cycle) rather
than requiring a historical-data API call.
"""
import logging
import time
from collections import deque

from core.models import ProposedOrder, Side, OrderType
from strategies.base_strategy import BaseStrategy

logger = logging.getLogger("groww_agent.strategy.ma_rsi")

SHORT_WINDOW = 9
LONG_WINDOW = 21
RSI_WINDOW = 14
RSI_ENTRY_MIN = 40
RSI_ENTRY_MAX = 70
RSI_EXIT_OVERBOUGHT = 75
STOP_LOSS_PCT = 2.0
DEFAULT_ORDER_QTY = 1

# Minimum relative gap between short/long MA required to count as a real
# crossover, not sub-tick price noise. Noise floor observed live on
# 2026-08-26 was ~0.0008% (RELIANCE oscillating 1304-1307); this is ~60x
# that floor, still well under RELIANCE's ~0.2% daily range.
MIN_CROSSOVER_GAP_PCT = 0.0005

# Minimum wall-clock time after closing a position before a new entry is
# allowed — kills the sub-minute flip-flops seen live (5s, 20s holds).
COOLDOWN_SECONDS = 60


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
    def __init__(self):
        self._state: dict[str, SymbolState] = {}

    def _get_state(self, symbol: str) -> SymbolState:
        if symbol not in self._state:
            self._state[symbol] = SymbolState()
        return self._state[symbol]

    def restore_position(self, symbol: str, entry_price: float) -> None:
        state = self._get_state(symbol)
        state.in_position = True
        state.entry_price = entry_price
        logger.info("%s: restored open position on startup, entry_price=%.2f", symbol, entry_price)

    def decide(self, symbol: str, last_traded_price: float | None) -> ProposedOrder | None:
        if last_traded_price is None:
            return None

        state = self._get_state(symbol)
        state.prices.append(last_traded_price)
        prices = list(state.prices)

        short_ma = _sma(prices, SHORT_WINDOW)
        long_ma = _sma(prices, LONG_WINDOW)
        rsi = _rsi(prices, RSI_WINDOW)

        if short_ma is None or long_ma is None or rsi is None:
            logger.info(
                "%s: warming up (%d/%d prices collected)",
                symbol, len(prices), max(SHORT_WINDOW, LONG_WINDOW, RSI_WINDOW + 1),
            )
            state.prev_short_ma, state.prev_long_ma = short_ma, long_ma
            return None

        gap_pct = abs(short_ma - long_ma) / long_ma

        crossed_up = (
            state.prev_short_ma is not None and state.prev_long_ma is not None
            and state.prev_short_ma <= state.prev_long_ma
            and short_ma > long_ma
            and gap_pct >= MIN_CROSSOVER_GAP_PCT
        )
        crossed_down = (
            state.prev_short_ma is not None and state.prev_long_ma is not None
            and state.prev_short_ma >= state.prev_long_ma
            and short_ma < long_ma
            and gap_pct >= MIN_CROSSOVER_GAP_PCT
        )

        order = None

        if not state.in_position:
            cooldown_active = (
                state.last_exit_time is not None
                and (time.monotonic() - state.last_exit_time) < COOLDOWN_SECONDS
            )
            if crossed_up and not cooldown_active and RSI_ENTRY_MIN <= rsi <= RSI_ENTRY_MAX:
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
                and last_traded_price <= state.entry_price * (1 - STOP_LOSS_PCT / 100)
            )
            if crossed_down:
                order = ProposedOrder(
                    symbol=symbol,
                    side=Side.SELL,
                    qty=DEFAULT_ORDER_QTY,
                    order_type=OrderType.MARKET,
                    reason=f"MA crossover DOWN (short={short_ma:.2f} < long={long_ma:.2f})",
                )
            elif rsi >= RSI_EXIT_OVERBOUGHT:
                order = ProposedOrder(
                    symbol=symbol,
                    side=Side.SELL,
                    qty=DEFAULT_ORDER_QTY,
                    order_type=OrderType.MARKET,
                    reason=f"RSI overbought ({rsi:.1f} >= {RSI_EXIT_OVERBOUGHT}) — taking profit",
                )
            elif stop_hit:
                order = ProposedOrder(
                    symbol=symbol,
                    side=Side.SELL,
                    qty=DEFAULT_ORDER_QTY,
                    order_type=OrderType.MARKET,
                    reason=(
                        f"Stop-loss hit: price {last_traded_price:.2f} <= "
                        f"entry {state.entry_price:.2f} * {1 - STOP_LOSS_PCT/100:.3f}"
                    ),
                )

            if order is not None:
                logger.info("%s: SELL signal — %s", symbol, order.reason)
                state.in_position = False
                state.entry_price = None
                state.last_exit_time = time.monotonic()

        state.prev_short_ma, state.prev_long_ma = short_ma, long_ma
        return order
