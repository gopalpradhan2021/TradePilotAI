"""
Wires everything together: strategy -> risk_manager -> execution, no bypass.

Orders, positions, and risk decisions are persisted immediately via
core/db/*_repo as they happen — this class holds no durable state of its
own, only the current-cycle LTP cache needed for the heartbeat file.
"""
import logging
import time
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from config.settings import Settings
from core.cost_model import calculate_order_charges, calculate_square_off_penalty
from core.db import orders_repo, positions_repo, risk_repo
from core.market_hours import is_market_open, is_past_square_off_cutoff
from core.models import OrderType, ProposedOrder, Segment, Side
from core.position_sizing import calculate_entry_qty
from core.notifier import send_notification
from core.reconciliation import reconcile_positions
from core.status_writer import write_heartbeat

logger = logging.getLogger("groww_agent.orchestrator")


class Orchestrator:
    _CIRCUIT_BREAKER_THRESHOLD = 5  # ~25s of continuous failure at the default 5s poll interval

    # Roadmap guidance: "every 30-60 seconds during live trading, subject to broker/API
    # limits." LIVE mode only — PaperBroker has no real broker position to reconcile against,
    # and this class constant makes that mode gate the only thing standing between a backtest
    # replay (which calls run_once() in a tight loop) and hammering a real API hundreds of
    # times, so it's deliberately not interval-skippable in any other way.
    _RECONCILE_INTERVAL_SEC = 60

    # How long a symbol's LTP can go unchanged during market hours before it's treated as
    # stale rather than genuinely flat. 2 minutes at the default 5s poll interval is ~24
    # consecutive identical reads — well past normal tick noise for a liquid large-cap, short
    # enough that a real feed outage doesn't go unnoticed for long.
    _STALE_DATA_THRESHOLD_SEC = 120

    # Groww's documented "Live Data" API category (quotes, LTP, OHLC, option chain — all of
    # it, rate-limited together, not per-endpoint) allows 10 req/s / 300 req/min. The CASH
    # LTP loop alone already spends ~180 of those 300 calls/min (15 symbols x 12 cycles/min
    # at the 5s poll interval) without issue, so 5s is a real, already-proven-safe floor for
    # this cadence too, not an arbitrary guess — matching it (rather than going faster than
    # CASH's own tick loop) keeps IV/OI/Greeks warmup as fast as the rate budget reasonably
    # allows: 2 underlyings x 12 cycles/min = 24 more calls/min, well inside the remaining
    # ~120/min headroom even accounting for jitter/overlap with the CASH loop's own bursts.
    _FNO_POLL_INTERVAL_SEC = 5

    # Candle fetches (broker.get_recent_candles()) are heavier than a single LTP call, and
    # MA/RSI trend signals don't need per-tick (5s) granularity — that granularity is exactly
    # what caused the noise problem candles replace. Coarser than _FNO_POLL_INTERVAL_SEC since
    # even the shortest supported candle interval (1minute) is still much wider than 5s.
    _CANDLE_FETCH_INTERVAL_SEC = 60

    # Delay between successive symbols' candle fetches within one _maybe_update_candles()
    # pass — see the comment at its call site for why (a real live rate-limit incident).
    _CANDLE_FETCH_STAGGER_SEC = 0.2

    # How often to check for CASH positions still open past Groww's real 3:20 PM IST MIS
    # auto-square-off cutoff. Cheap (a time check + one DB query per symbol) — no need for
    # tick-level granularity like the LTP loop.
    _SQUARE_OFF_CHECK_INTERVAL_SEC = 60

    def __init__(self, settings: Settings, broker, risk_manager, strategy,
                 fno_strategy=None, fno_market_data=None,
                 clock: Callable[[], float] | None = None,
                 now_ist_fn: Callable[[], datetime] | None = None):
        """fno_strategy/fno_market_data are both optional and None by default — an
        Orchestrator built without them (every existing CASH-only caller) never touches
        the FNO code path at all; run_once_fno()/_maybe_run_fno_cycle() no-op immediately
        if fno_strategy is None.

        clock defaults to the real monotonic clock (resolved at construction time, matching
        MARsiStrategy's own clock param) — core/backtest_engine.py injects a simulated one
        derived from replayed candle timestamps, so periodic cadences (reconciliation, FNO
        cycle, candle fetch) advance with simulated bar time instead of real wall-clock time
        during a backtest run.

        now_ist_fn defaults to the real wall-clock IST instant, used only for the MIS
        square-off cutoff check — core/backtest_engine.py injects SimClock.now_ist instead,
        so a backtest run after 3:20 PM real IST time doesn't force-close every open
        position on its very next bar regardless of which historical date is actually being
        replayed (found live 2026-09-01 — see is_past_square_off_cutoff()'s docstring)."""
        self.settings = settings
        self.broker = broker
        self.risk_manager = risk_manager
        self.strategy = strategy
        self.fno_strategy = fno_strategy
        self.fno_market_data = fno_market_data
        self._clock = clock if clock is not None else time.monotonic
        self._now_ist_fn = now_ist_fn if now_ist_fn is not None else (
            lambda: datetime.now(ZoneInfo("Asia/Kolkata"))
        )
        self._last_ltp: dict[str, float | None] = {}
        self._consecutive_failures = 0
        # Starts the clock at construction rather than None/0, so the first periodic check
        # doesn't fire immediately after startup — main.py already does a startup-time
        # reconciliation before the orchestrator loop even begins.
        self._last_reconcile_time = self._clock()
        self._last_fno_cycle_time = self._clock()
        self._last_candle_fetch_time = self._clock()
        self._last_square_off_check_time = self._clock()
        # {symbol: (last_seen_price, monotonic_time_that_price_was_first_seen)} — tracks how
        # long each symbol's LTP has gone UNCHANGED, not just how long since the last fetch,
        # since a degraded feed returning the same cached value on every successful call is a
        # real, silent failure mode (fetch errors already return None and are handled
        # separately by strategy.decide()).
        self._price_freshness: dict[str, tuple[float, float]] = {}
        self._stale_notified: set[str] = set()

    def _notify(self, message: str):
        try:
            send_notification(self.settings, message)
        except Exception as e:
            logger.error("Failed to send notification: %s", e)

    def _maybe_reconcile(self, symbols: list[str]):
        """LIVE mode only, at most once per _RECONCILE_INTERVAL_SEC — same check
        main.py runs once at startup, repeated periodically so drift mid-session (a fill
        the bot didn't hear about, a manual trade placed directly on Groww's app, etc.)
        gets caught before more orders are proposed on top of a wrong local picture,
        instead of only ever being caught on the next restart."""
        if self.settings.mode != "LIVE":
            return
        now = self._clock()
        if now - self._last_reconcile_time < self._RECONCILE_INTERVAL_SEC:
            return
        self._last_reconcile_time = now
        reconcile_positions(self.broker, self.risk_manager, symbols, logger)

    def _maybe_run_fno_cycle(self, fno_underlyings: list[str] | None):
        """At most once per _FNO_POLL_INTERVAL_SEC, and only if an fno_strategy was
        configured — a no-op for every existing CASH-only Orchestrator."""
        if self.fno_strategy is None or not fno_underlyings:
            return
        now = self._clock()
        if now - self._last_fno_cycle_time < self._FNO_POLL_INTERVAL_SEC:
            return
        self._last_fno_cycle_time = now
        self.run_once_fno(fno_underlyings)

    def _maybe_update_candles(self, symbols: list[str]):
        """At most once per _CANDLE_FETCH_INTERVAL_SEC. No-op if self.strategy doesn't
        declare candle requirements (get_candle_requirements() returns None, the default) —
        every CASH strategy other than MARsiStrategy is unaffected. A per-symbol fetch
        failure (get_recent_candles() returns None) just skips that symbol this cycle; the
        strategy keeps trading off whatever candle series it already has."""
        requirements = self.strategy.get_candle_requirements()
        if requirements is None:
            return
        interval, lookback_bars = requirements
        now = self._clock()
        if now - self._last_candle_fetch_time < self._CANDLE_FETCH_INTERVAL_SEC:
            return
        self._last_candle_fetch_time = now
        for i, symbol in enumerate(symbols):
            # Staggered, not back-to-back: found live 2026-08-31 that firing all N candle
            # fetches with zero delay between them (previously the case here) can burst past
            # Groww's "Live Data" API category's per-second rate limit (shared with the CASH
            # LTP loop) — repeated "Rate limit has breached" errors on candle fetches
            # specifically. 0.2s between calls keeps any 1-second window to ~5 of these
            # calls, comfortably under the limit even overlapping with the LTP loop's own
            # calls. Skipped before the first call — no need to delay entering the loop.
            if i > 0:
                time.sleep(self._CANDLE_FETCH_STAGGER_SEC)
            candles = self.broker.get_recent_candles(symbol, interval=interval, lookback_bars=lookback_bars)
            if candles is None:
                logger.warning("%s: candle fetch failed this cycle — keeping existing series.", symbol)
                continue
            self.strategy.update_candles(symbol, candles)

    def _maybe_square_off_mis_positions(self, symbols: list[str]):
        """At most once per _SQUARE_OFF_CHECK_INTERVAL_SEC, and only past Groww's real
        3:20 PM IST MIS cutoff — force-closes any CASH position still open, mirroring the
        mandatory broker-side square-off a real MIS order (core/execution.py's
        _groww_product(), CASH always PRODUCT_MIS) is subject to. Routes the closing SELL
        through the same _handle_proposed_order() -> risk_manager.check() ->
        broker.place_order() funnel as every other order — no bypass, even for a forced
        exit."""
        now = self._clock()
        if now - self._last_square_off_check_time < self._SQUARE_OFF_CHECK_INTERVAL_SEC:
            return
        self._last_square_off_check_time = now

        if not is_past_square_off_cutoff(self._now_ist_fn()):
            return

        for symbol in symbols:
            open_pos = positions_repo.get_open_position(symbol)
            if open_pos is None or open_pos.get("segment", "CASH") != "CASH":
                continue
            ltp = self.broker.get_ltp(symbol) or self._last_ltp.get(symbol)
            order = ProposedOrder(
                symbol=symbol, side=Side.SELL, qty=open_pos["qty"], order_type=OrderType.MARKET,
                reason="AUTO_SQUARE_OFF: MIS position force-closed at broker's 3:20 PM IST cutoff",
            )
            self._handle_proposed_order(order, ltp, extra_charges=calculate_square_off_penalty())
            if positions_repo.get_open_position(symbol) is None:
                self.strategy.force_exit(symbol)
                logger.info("%s: MIS position auto-squared-off (3:20 PM IST cutoff).", symbol)

    def run_once_fno(self, underlyings: list[str]):
        for underlying in underlyings:
            chain = self.fno_market_data.get_chain(underlying)
            if chain is None:
                continue
            proposed = self.fno_strategy.decide_fno(underlying=underlying, chain=chain)
            if proposed is None:
                continue
            # The contract's OWN premium, not chain.underlying_ltp — the underlying's spot
            # price (e.g. NIFTY ~24000) is nowhere close to an option's premium (~100s) and
            # must never be used as the order's reference price for risk-sizing or fill.
            quote = chain.find_quote(proposed.symbol)
            contract_ltp = quote.ltp if quote is not None else None
            # Same funnel CASH orders already go through — _handle_proposed_order() ->
            # risk_manager.check() -> broker.place_order() — no bypass for FNO either.
            self._handle_proposed_order(proposed, contract_ltp)

    def _is_stale(self, symbol: str, ltp: float | None) -> bool:
        """Updates freshness tracking for `symbol` and returns whether its price should be
        treated as too stale to trade on right now. Only checked during market hours — a
        frozen price outside market hours is expected, not a fault."""
        if ltp is None:
            return False  # already handled as a no-decision case by strategy.decide()

        now = time.monotonic()
        prev = self._price_freshness.get(symbol)
        if prev is None or prev[0] != ltp:
            self._price_freshness[symbol] = (ltp, now)
            self._stale_notified.discard(symbol)
            return False

        _, first_seen_at = prev
        if now - first_seen_at < self._STALE_DATA_THRESHOLD_SEC:
            return False
        if not is_market_open():
            return False

        if symbol not in self._stale_notified:
            self._stale_notified.add(symbol)
            logger.warning(
                "%s: price unchanged for over %ds during market hours (stuck at %s) — "
                "treating as stale, skipping trading decisions until it moves again.",
                symbol, self._STALE_DATA_THRESHOLD_SEC, ltp,
            )
            self._notify(
                f"⚠️ {symbol}: price feed looks stale (unchanged for over "
                f"{self._STALE_DATA_THRESHOLD_SEC}s during market hours) — skipping trades "
                f"on this symbol until fresh data resumes."
            )
        return True

    def run_once(self, symbols: list[str], fno_underlyings: list[str] | None = None):
        self.risk_manager.refresh_halt_state()
        if self.risk_manager.halted:
            logger.warning("Skipping cycle — risk manager is halted: %s",
                            self.risk_manager.halt_reason)
            self._write_heartbeat(symbols, fno_underlyings)
            return

        self._maybe_reconcile(symbols)
        if self.risk_manager.halted:
            logger.warning("Skipping cycle — reconciliation just halted trading: %s",
                            self.risk_manager.halt_reason)
            self._write_heartbeat(symbols, fno_underlyings)
            return

        self._maybe_update_candles(symbols)
        self._maybe_square_off_mis_positions(symbols)

        for symbol in symbols:
            ltp = self.broker.get_ltp(symbol)
            self._last_ltp[symbol] = ltp
            if self._is_stale(symbol, ltp):
                continue
            proposed = self.strategy.decide(symbol=symbol, last_traded_price=ltp)
            if proposed is None:
                continue

            self._handle_proposed_order(proposed, ltp)

        self._maybe_run_fno_cycle(fno_underlyings)

        self._write_heartbeat(symbols, fno_underlyings)

    def _write_heartbeat(self, symbols: list[str], fno_underlyings: list[str] | None = None):
        try:
            strategy_debug = {symbol: self.strategy.get_debug_info(symbol) for symbol in symbols}
            fno_strategy_debug = (
                {u: self.fno_strategy.get_debug_info(u) for u in fno_underlyings}
                if self.fno_strategy is not None and fno_underlyings else {}
            )
            write_heartbeat(
                mode=self.settings.mode,
                halted=self.risk_manager.halted,
                halt_reason=self.risk_manager.halt_reason,
                symbols=symbols,
                last_ltp=self._last_ltp,
                strategy_debug=strategy_debug,
                fno_underlyings=fno_underlyings or [],
                fno_strategy_debug=fno_strategy_debug,
            )
        except Exception as e:
            logger.error("Failed to write heartbeat: %s", e)

    def _handle_proposed_order(self, order: ProposedOrder, ltp: float | None,
                                extra_charges: float = 0.0):
        ref_price = order.limit_price or ltp
        # Capital-aware CASH sizing — the strategy's qty (DEFAULT_ORDER_QTY, a nominal
        # placeholder) gets overwritten here, before the order is ever persisted, so the
        # audit trail and risk_manager.check() both see the FINAL qty, never a placeholder.
        # FNO is untouched — IvOiStrategy manages its own lot-based qty/lot_size directly.
        # Guarded on ref_price is not None: a future CASH strategy or a LIMIT order with no
        # limit_price and a failed LTP fetch could reach here with nothing to size against —
        # skip resize and let risk_manager.check()'s existing "No reference price available"
        # rejection handle it unresized, same as today.
        if order.segment == Segment.CASH and ref_price is not None:
            if order.side == Side.BUY:
                available_capital = self.settings.risk.total_capital_inr - positions_repo.get_deployed_capital()
                order.qty = calculate_entry_qty(
                    ref_price, available_capital,
                    self.settings.risk.max_order_value_inr, self.settings.risk.max_position_qty,
                )
            else:
                # SELL must always close the REAL held qty, never re-derive/guess it — this
                # is also what keeps exit charges (calculate_order_charges below) correct,
                # not just the broker order quantity.
                open_pos = positions_repo.get_open_position(order.symbol)
                if open_pos is not None:
                    order.qty = open_pos["qty"]
                # else: leave order.qty unresized — matches the existing defensive
                # `else: pnl_delta = 0.0` fallback later in this method.
        try:
            order_id = orders_repo.insert_order(order, status="PROPOSED", reference_price=ref_price)
        except orders_repo.DuplicateIdempotencyKeyError:
            reason = "Duplicate order (idempotency key already used)."
            logger.warning("Order blocked: %s | order=%s", reason, order)
            risk_repo.record_risk_event(
                order_id=None, symbol=order.symbol, event_type="CHECK_REJECTED",
                reasons=[reason], reference_price=ref_price,
            )
            return

        check = self.risk_manager.check(order, last_traded_price=ltp, order_id=order_id)
        if not check.approved:
            logger.warning("Order blocked: %s | reasons=%s", order, check.reasons)
            orders_repo.update_order_status(
                order_id, status="BLOCKED", message="; ".join(check.reasons)
            )
            return

        result = self.broker.place_order(order, last_traded_price=ltp)
        logger.info("Execution result: %s", result)
        margin_used = check.margin_quote.required_margin if check.margin_quote else None
        # Real transaction costs (core/cost_model.py) — CASH only, FNO has a different fee
        # structure and isn't modeled. Computed once here and reused for both the order
        # audit row and the position row below, so PAPER-mode P&L reflects what a real fill
        # would actually cost, not just the raw price move.
        charges = None
        if result.status == "FILLED" and result.fill_price is not None and order.segment == Segment.CASH:
            charges = calculate_order_charges(result.fill_price * order.qty, order.side) + extra_charges
        orders_repo.update_order_status(
            order_id,
            status=result.status,
            broker_order_id=result.broker_order_id,
            fill_price=result.fill_price,
            message=result.message,
            margin_used=margin_used,
            charges=charges,
        )

        if result.status == "FILLED" and result.fill_price is not None:
            order_value = result.fill_price * order.qty
            self._notify(
                f"✅ FILLED {order.side.value} {order.qty} {order.symbol} "
                f"@ ₹{result.fill_price:.2f} (mode={self.settings.mode})"
            )

            if order.side == Side.BUY:
                positions_repo.open_position(
                    symbol=order.symbol, qty=order.qty,
                    entry_price=result.fill_price, entry_order_id=order_id,
                    segment=order.segment.value, underlying_symbol=order.underlying_symbol,
                    margin_used=margin_used, entry_charges=charges or 0.0,
                )
                self.risk_manager.record_fill(
                    side=order.side, order_value=order_value, pnl_delta=0.0, order_id=order_id
                )
            else:
                open_pos = positions_repo.get_open_position(order.symbol)
                if open_pos is not None:
                    pnl_delta = positions_repo.close_position(
                        symbol=order.symbol, exit_price=result.fill_price, exit_order_id=order_id,
                        exit_charges=charges or 0.0,
                    )
                else:
                    pnl_delta = 0.0
                self.risk_manager.record_fill(
                    side=order.side, order_value=order_value, pnl_delta=pnl_delta, order_id=order_id
                )
        else:
            self._notify(
                f"⚠️ Order NOT filled: {order.side.value} {order.qty} {order.symbol} "
                f"status={result.status} message={result.message} (mode={self.settings.mode})"
            )

    def _run_cycle(self, symbols: list[str], fno_underlyings: list[str] | None = None):
        try:
            self.run_once(symbols, fno_underlyings)
            self._consecutive_failures = 0
        except Exception as e:
            self._consecutive_failures += 1
            logger.exception("Unhandled error in run_once (%d consecutive): %s",
                              self._consecutive_failures, e)
            self._notify(
                f"🔴 groww-bot crashed in run_once ({self._consecutive_failures} consecutive): {e}"
            )
            if self._consecutive_failures >= self._CIRCUIT_BREAKER_THRESHOLD:
                logger.critical("Circuit breaker tripped after %d consecutive failures — halting.",
                                 self._consecutive_failures)
                self.risk_manager.halt_circuit_breaker(self._consecutive_failures)

    def run_forever(self, symbols: list[str], poll_interval_sec: int = 5,
                     fno_underlyings: list[str] | None = None):
        logger.info("Starting orchestrator loop | mode=%s | symbols=%s | fno_underlyings=%s",
                    self.settings.mode, symbols, fno_underlyings)
        while True:
            self._run_cycle(symbols, fno_underlyings)
            time.sleep(poll_interval_sec)
