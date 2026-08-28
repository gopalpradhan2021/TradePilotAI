"""
Wires everything together: strategy -> risk_manager -> execution, no bypass.

Orders, positions, and risk decisions are persisted immediately via
core/db/*_repo as they happen — this class holds no durable state of its
own, only the current-cycle LTP cache needed for the heartbeat file.
"""
import logging
import time

from config.settings import Settings
from core.db import orders_repo, positions_repo, risk_repo
from core.market_hours import is_market_open
from core.models import ProposedOrder, Side
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

    # Option-chain fetches are heavier than a single LTP call, and IV/OI/Greeks don't need
    # per-tick (5s) granularity the way a crossover signal does — gated separately from the
    # CASH poll interval, similar in spirit to _RECONCILE_INTERVAL_SEC.
    _FNO_POLL_INTERVAL_SEC = 30

    def __init__(self, settings: Settings, broker, risk_manager, strategy,
                 fno_strategy=None, fno_market_data=None):
        """fno_strategy/fno_market_data are both optional and None by default — an
        Orchestrator built without them (every existing CASH-only caller) never touches
        the FNO code path at all; run_once_fno()/_maybe_run_fno_cycle() no-op immediately
        if fno_strategy is None."""
        self.settings = settings
        self.broker = broker
        self.risk_manager = risk_manager
        self.strategy = strategy
        self.fno_strategy = fno_strategy
        self.fno_market_data = fno_market_data
        self._last_ltp: dict[str, float | None] = {}
        self._consecutive_failures = 0
        # Starts the clock at construction rather than None/0, so the first periodic check
        # doesn't fire immediately after startup — main.py already does a startup-time
        # reconciliation before the orchestrator loop even begins.
        self._last_reconcile_time = time.monotonic()
        self._last_fno_cycle_time = time.monotonic()
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
        now = time.monotonic()
        if now - self._last_reconcile_time < self._RECONCILE_INTERVAL_SEC:
            return
        self._last_reconcile_time = now
        reconcile_positions(self.broker, self.risk_manager, symbols, logger)

    def _maybe_run_fno_cycle(self, fno_underlyings: list[str] | None):
        """At most once per _FNO_POLL_INTERVAL_SEC, and only if an fno_strategy was
        configured — a no-op for every existing CASH-only Orchestrator."""
        if self.fno_strategy is None or not fno_underlyings:
            return
        now = time.monotonic()
        if now - self._last_fno_cycle_time < self._FNO_POLL_INTERVAL_SEC:
            return
        self._last_fno_cycle_time = now
        self.run_once_fno(fno_underlyings)

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

    def _handle_proposed_order(self, order: ProposedOrder, ltp: float | None):
        ref_price = order.limit_price or ltp
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
        orders_repo.update_order_status(
            order_id,
            status=result.status,
            broker_order_id=result.broker_order_id,
            fill_price=result.fill_price,
            message=result.message,
            margin_used=margin_used,
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
                    margin_used=margin_used,
                )
                self.risk_manager.record_fill(
                    side=order.side, order_value=order_value, pnl_delta=0.0, order_id=order_id
                )
            else:
                open_pos = positions_repo.get_open_position(order.symbol)
                if open_pos is not None:
                    pnl_delta = positions_repo.close_position(
                        symbol=order.symbol, exit_price=result.fill_price, exit_order_id=order_id
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
