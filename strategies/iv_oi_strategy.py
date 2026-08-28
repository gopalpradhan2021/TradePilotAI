"""
Implied Volatility + Open Interest strategy for index F&O options (Phase B).

Entry (BUY CE or PE): the near-ATM strike on one side (CE or PE) shows a real OI
buildup between consecutive snapshots (not just poll-to-poll noise), its delta sits in
a genuinely directional band (not deep ITM/OTM), and current IV isn't already at the
top of this underlying's own recently-observed range (avoids paying peak premium).

Exit (SELL, closing the held contract): the contract's delta has decayed past a floor
(lost directional relevance), OR its IV has collapsed from entry (the classic "IV
crush" that erodes a long option's value regardless of underlying direction), OR a
stop-loss on the option's own premium is hit.

Maintains its own rolling IV history and previous-snapshot OI per underlying (fed by
whatever OptionChainSnapshot core/fno_market_data.py resolves each cycle) — the F&O
counterpart to MARsiStrategy's rolling price window. There is no historical
option-chain API to derive a real IV percentile from (see the plan's live research),
so "IV elevated" here means elevated relative to this strategy's own recent
observations since it started running, not a true multi-year percentile.
"""
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from core.models import (
    OptionChainSnapshot, OptionQuote, OptionType, OrderType, ProposedOrder,
    Segment, Side, StrikeQuote,
)
from strategies.base_strategy import BaseStrategy

logger = logging.getLogger("groww_agent.strategy.iv_oi")

# How many recent IV readings to keep per underlying before the strategy is considered
# warmed up — mirrors MARsiStrategy's SHORT_WINDOW/LONG_WINDOW warmup gate.
IV_HISTORY_LEN = 50

# Minimum relative OI increase (vs the previous snapshot) at a strike to count as a real
# buildup. No live-observed noise floor exists yet for OI (Phase B is the first time OI
# is tracked at all here) — start conservative and tune from real paper-mode data, the
# same way MARsiStrategy's MIN_CROSSOVER_GAP_PCT was tuned after seeing live noise.
MIN_OI_BUILDUP_PCT = 5.0

# Delta band for a "genuinely directional" near-ATM contract — deep ITM (delta near 1)
# barely trades differently from just holding the underlying; deep OTM (delta near 0)
# is mostly a lottery ticket. 0.35-0.65 brackets near-ATM on either side.
ENTRY_DELTA_MIN = 0.35
ENTRY_DELTA_MAX = 0.65

# Exit: held contract's delta has decayed below this magnitude — no longer meaningfully
# tracking the underlying's direction.
EXIT_DELTA_FLOOR = 0.15

# Exit: current IV has fallen this many relative percent from entry IV — a long option's
# value erodes from IV collapse independent of the underlying's own direction.
IV_COLLAPSE_EXIT_PCT = 25.0

# On the option's own premium, not the underlying — options move far more per point than
# their underlying, so this is deliberately much wider than MARsiStrategy's 2% CASH stop.
STOP_LOSS_PCT = 30.0

DEFAULT_LOT_QTY = 1  # lots, not units — total_units = qty * lot_size (see core/models.py)


@dataclass(frozen=True)
class IvOiParams:
    iv_history_len: int = IV_HISTORY_LEN
    min_oi_buildup_pct: float = MIN_OI_BUILDUP_PCT
    entry_delta_min: float = ENTRY_DELTA_MIN
    entry_delta_max: float = ENTRY_DELTA_MAX
    exit_delta_floor: float = EXIT_DELTA_FLOOR
    iv_collapse_exit_pct: float = IV_COLLAPSE_EXIT_PCT
    stop_loss_pct: float = STOP_LOSS_PCT


class UnderlyingState:
    def __init__(self, iv_history_len: int = IV_HISTORY_LEN):
        self.atm_iv_history: deque[float] = deque(maxlen=iv_history_len)
        self.prev_oi: dict[str, int] = {}  # trading_symbol -> OI, from the previous snapshot
        self.in_position = False
        self.held_trading_symbol: str | None = None
        self.held_option_type: OptionType | None = None
        self.held_strike: float | None = None
        self.entry_price: float | None = None
        self.entry_iv: float | None = None
        self.lot_size: int | None = None


def _find_atm_strike(snapshot: OptionChainSnapshot) -> StrikeQuote | None:
    if not snapshot.strikes:
        return None
    return min(snapshot.strikes, key=lambda s: abs(s.strike - snapshot.underlying_ltp))


class IvOiStrategy(BaseStrategy):
    def __init__(self, clock: Callable[[], float] | None = None,
                 params: IvOiParams | None = None,
                 lot_size_fn: Callable[[str], int | None] | None = None):
        """clock/params mirror MARsiStrategy's own constructor exactly (real monotonic
        clock resolved at construction time so tests can monkeypatch time.monotonic
        beforehand; params defaults to the production constants above).

        lot_size_fn resolves a trading_symbol to its real, authoritative lot size (see
        Broker.get_lot_size — confirmed live this genuinely varies over time, e.g. NIFTY
        was 65 not the commonly-assumed 75, so it must never be hardcoded). If None,
        decide_fno() logs and skips any entry signal rather than guessing a lot size."""
        self._state: dict[str, UnderlyingState] = {}
        self._clock = clock if clock is not None else time.monotonic
        self.params = params if params is not None else IvOiParams()
        self._lot_size_fn = lot_size_fn

    def _get_state(self, underlying: str) -> UnderlyingState:
        if underlying not in self._state:
            self._state[underlying] = UnderlyingState(self.params.iv_history_len)
        return self._state[underlying]

    def restore_position(self, symbol: str, entry_price: float) -> None:
        # `symbol` here is the underlying (main.py calls this once per watched underlying,
        # mirroring how it already calls MARsiStrategy.restore_position per CASH symbol).
        # Which exact contract (strike/CE-PE) was held isn't recoverable from just
        # (underlying, entry_price) — this only marks in_position so a duplicate entry
        # isn't proposed; it deliberately does NOT let decide_fno() manage this position's
        # exits, since it can't identify which contract to evaluate.
        state = self._get_state(symbol)
        state.in_position = True
        state.entry_price = entry_price
        logger.warning(
            "%s: restored open FNO position on startup (entry_price=%.2f) but the exact "
            "held contract is not recoverable from this alone — decide_fno() will not "
            "manage this position's exit until it's manually reconciled.",
            symbol, entry_price,
        )

    def get_debug_info(self, symbol: str) -> dict:
        state = self._get_state(symbol)
        return {
            "warmed_up": len(state.atm_iv_history) >= self.params.iv_history_len,
            "iv_history_collected": len(state.atm_iv_history),
            "iv_history_needed": self.params.iv_history_len,
            "in_position": state.in_position,
            "held_trading_symbol": state.held_trading_symbol,
            "held_strike": state.held_strike,
            "entry_price": state.entry_price,
            "entry_iv": state.entry_iv,
        }

    def decide_fno(self, underlying: str, chain: OptionChainSnapshot) -> ProposedOrder | None:
        state = self._get_state(underlying)
        atm = _find_atm_strike(chain)
        if atm is None:
            return None

        # Track ATM IV off whichever side has data (prefer CE, fall back to PE) — used
        # only as "is IV elevated relative to what we've recently seen", not tied to a
        # specific side.
        atm_iv = None
        if atm.ce is not None:
            atm_iv = atm.ce.greeks.iv
        elif atm.pe is not None:
            atm_iv = atm.pe.greeks.iv
        if atm_iv is not None:
            state.atm_iv_history.append(atm_iv)

        if not state.in_position:
            order = self._maybe_enter(underlying, atm, state)
        else:
            order = self._maybe_exit(underlying, chain, state)

        # Update prev_oi AFTER using it for this cycle's buildup detection, for every
        # strike currently in the chain — mirrors MARsiStrategy updating
        # prev_short_ma/prev_long_ma at the end of decide().
        new_oi: dict[str, int] = {}
        for s in chain.strikes:
            if s.ce is not None:
                new_oi[s.ce.trading_symbol] = s.ce.open_interest
            if s.pe is not None:
                new_oi[s.pe.trading_symbol] = s.pe.open_interest
        state.prev_oi = new_oi

        return order

    def _oi_buildup_pct(self, state: UnderlyingState, trading_symbol: str,
                         current_oi: int) -> float | None:
        prev = state.prev_oi.get(trading_symbol)
        if not prev:  # None (first time seeing this contract) or 0 (avoid div-by-zero)
            return None
        return (current_oi - prev) / prev * 100

    def _maybe_enter(self, underlying: str, atm: StrikeQuote,
                      state: UnderlyingState) -> ProposedOrder | None:
        p = self.params
        if len(state.atm_iv_history) < p.iv_history_len:
            logger.info("%s: warming up IV history (%d/%d)", underlying,
                        len(state.atm_iv_history), p.iv_history_len)
            return None

        # Reject entries when today's IV is at/above the highest seen in this rolling
        # window (including today) — the most expensive premium relative to what this
        # underlying has recently offered, the opposite of what a long-option buyer wants.
        current_iv = state.atm_iv_history[-1]
        iv_ceiling = max(state.atm_iv_history)
        if current_iv >= iv_ceiling:
            return None

        # CE checked before PE — a deliberate, deterministic tie-break if both sides
        # happen to qualify on the same cycle, not a directional bias.
        for side_quote, option_type in ((atm.ce, OptionType.CE), (atm.pe, OptionType.PE)):
            order = self._maybe_enter_side(underlying, atm, side_quote, option_type, state, current_iv, iv_ceiling)
            if order is not None:
                return order
        return None

    def _maybe_enter_side(self, underlying: str, atm: StrikeQuote, side_quote: OptionQuote | None,
                           option_type: OptionType, state: UnderlyingState,
                           current_iv: float, iv_ceiling: float) -> ProposedOrder | None:
        p = self.params
        if side_quote is None or side_quote.ltp is None:
            return None

        delta = abs(side_quote.greeks.delta)
        if not (p.entry_delta_min <= delta <= p.entry_delta_max):
            return None

        buildup = self._oi_buildup_pct(state, side_quote.trading_symbol, side_quote.open_interest)
        if buildup is None or buildup < p.min_oi_buildup_pct:
            return None

        if self._lot_size_fn is None:
            logger.error(
                "%s: entry signal on %s but no lot_size_fn configured — refusing to guess "
                "a lot size, skipping this signal.", underlying, side_quote.trading_symbol,
            )
            return None
        lot_size = self._lot_size_fn(side_quote.trading_symbol)
        if lot_size is None:
            logger.error(
                "%s: entry signal on %s but lot size fetch failed — refusing to guess, "
                "skipping this signal.", underlying, side_quote.trading_symbol,
            )
            return None

        order = ProposedOrder(
            symbol=side_quote.trading_symbol,
            underlying_symbol=underlying,
            side=Side.BUY,
            qty=DEFAULT_LOT_QTY,
            order_type=OrderType.MARKET,
            segment=Segment.FNO,
            lot_size=lot_size,
            strike_price=atm.strike,
            option_type=option_type,
            reason=(
                f"OI buildup {buildup:.1f}% on {option_type.value} at strike {atm.strike} "
                f"(delta={delta:.2f}), IV={current_iv:.2f} below recent high {iv_ceiling:.2f}"
            ),
        )
        state.in_position = True
        state.held_trading_symbol = side_quote.trading_symbol
        state.held_option_type = option_type
        state.held_strike = atm.strike
        state.entry_price = side_quote.ltp
        state.entry_iv = current_iv
        state.lot_size = lot_size
        logger.info("%s: BUY signal — %s", underlying, order.reason)
        return order

    def _maybe_exit(self, underlying: str, chain: OptionChainSnapshot,
                     state: UnderlyingState) -> ProposedOrder | None:
        p = self.params
        held = chain.find_quote(state.held_trading_symbol)
        if held is None:
            logger.warning(
                "%s: holding %s but it's not in the current chain snapshot (expiry may "
                "have rolled) — cannot evaluate exit this cycle.",
                underlying, state.held_trading_symbol,
            )
            return None

        delta = abs(held.greeks.delta)
        exit_reason = None
        if delta < p.exit_delta_floor:
            exit_reason = f"Delta decayed to {delta:.2f} (floor {p.exit_delta_floor})"
        elif state.entry_iv and held.greeks.iv <= state.entry_iv * (1 - p.iv_collapse_exit_pct / 100):
            exit_reason = (
                f"IV collapsed {held.greeks.iv:.2f} <= entry {state.entry_iv:.2f} "
                f"* {1 - p.iv_collapse_exit_pct / 100:.2f}"
            )
        elif (held.ltp is not None and state.entry_price
              and held.ltp <= state.entry_price * (1 - p.stop_loss_pct / 100)):
            exit_reason = (
                f"Stop-loss: premium {held.ltp:.2f} <= entry {state.entry_price:.2f} "
                f"* {1 - p.stop_loss_pct / 100:.2f}"
            )

        if exit_reason is None:
            return None

        order = ProposedOrder(
            symbol=state.held_trading_symbol,
            underlying_symbol=underlying,
            side=Side.SELL,
            qty=DEFAULT_LOT_QTY,
            order_type=OrderType.MARKET,
            segment=Segment.FNO,
            lot_size=state.lot_size or 1,
            strike_price=state.held_strike,
            option_type=state.held_option_type,
            reason=exit_reason,
        )
        logger.info("%s: SELL signal — %s", underlying, exit_reason)
        state.in_position = False
        state.held_trading_symbol = None
        state.held_option_type = None
        state.held_strike = None
        state.entry_price = None
        state.entry_iv = None
        state.lot_size = None
        return order
