"""
Every proposed order MUST pass through RiskManager.check() before it can
reach the execution layer.

Daily counters (trade count, realized P&L) and the halt flag are persisted
in SQLite (see core/db/) rather than held purely in memory, so a halt or a
day's trade count survives a process restart. Deployed capital is not
tracked as running state at all — it's derived from open positions on every
read, so it can't drift from reality across a crash.
"""
import logging
from datetime import date
from typing import Callable

from config.settings import RiskConfig
from core.db import positions_repo, risk_repo
from core.models import ProposedOrder, RiskCheckResult, Segment, Side
from core.notifier import send_notification_raw

logger = logging.getLogger("groww_agent.risk")


class RiskManager:
    def __init__(self, risk_config: RiskConfig, ntfy_topic: str = "",
                 today_fn: Callable[[], date] = date.today, mode: str = "LIVE",
                 margin_provider=None):
        """today_fn defaults to the real wall-clock date; scripts/backtest.py injects a
        simulated clock instead, so day-rollover (and the daily counters it resets) tracks
        simulated historical days rather than the backtest process's real run time.

        mode defaults to "LIVE" (the conservative choice — enforce every cap unless told
        otherwise) so existing callers that don't pass it keep today's behavior unchanged.
        main.py passes the real settings.mode; when it's "PAPER", max_trades_per_day stops
        being enforced (see check()) — paper trading risks no real money, so the cap exists
        only to make the strategy's true, unconstrained trade frequency visible for future
        tuning, not to protect anything. LIVE mode always enforces it regardless of what's
        passed here as a defense against a mistaken/missing mode value.

        margin_provider (core.margin_provider.GrowwMarginProvider, optional) sources real
        SPAN+exposure margin for FNO orders instead of the naive price*qty math used for
        CASH — see the FNO branch in check(). None by default so CASH-only callers (and
        Phase B development before this is wired into main.py) are unaffected."""
        self.cfg = risk_config
        self._ntfy_topic = ntfy_topic
        self._today_fn = today_fn
        self.mode = mode
        self._margin_provider = margin_provider
        self._current_day = self._today_fn()
        self._sync_daily_state()

    def _notify(self, message: str):
        try:
            send_notification_raw(self._ntfy_topic, message)
        except Exception as e:
            logger.error("Failed to send notification: %s", e)

    def _sync_daily_state(self):
        summary = risk_repo.get_or_create_daily_summary(self._current_day.isoformat())
        self._trades_today = summary["trades_count"]
        self._realized_pnl_today = summary["realized_pnl"]
        self.halted = bool(summary["halted"])
        self.halt_reason = summary["halt_reason"]
        self.halt_source = summary.get("halt_source", "AUTO")

    def _roll_day_if_needed(self):
        today = self._today_fn()
        if today != self._current_day:
            self._current_day = today
            self._sync_daily_state()
            logger.info("New trading day — risk counters reset.")

    def refresh_halt_state(self):
        """Re-reads halted/halt_reason/halt_source from the DB unconditionally, so a halt
        or resume triggered by a separate process (scripts/halt_bot.py, scripts/resume_bot.py)
        takes effect on this process within one call, not just on day rollover. Rolls the day
        first — otherwise a stale halt from a prior day could read as still-halted on a day
        where check() (the other place that rolls the day) hasn't run yet."""
        self._roll_day_if_needed()
        db_state = risk_repo.get_halt_state(self._current_day.isoformat())
        self.halted = bool(db_state["halted"])
        self.halt_reason = db_state["halt_reason"]
        self.halt_source = db_state["halt_source"]

    @property
    def _deployed_capital(self) -> float:
        return positions_repo.get_deployed_capital()

    def _halt(self, reason: str, source: str, log_prefix: str = "TRADING HALTED"):
        self.halted = True
        self.halt_reason = reason
        self.halt_source = source
        risk_repo.set_halted(self._current_day.isoformat(), True, reason, source=source)
        risk_repo.record_risk_event(
            order_id=None, symbol=None, event_type="HALTED", reasons=[reason],
        )
        logger.critical("%s: %s", log_prefix, reason)
        self._notify(f"🔴 {log_prefix}: {reason}")

    def record_fill(self, side: Side, order_value: float, pnl_delta: float = 0.0,
                     order_id: int | None = None):
        trade_date = self._current_day.isoformat()
        risk_repo.increment_daily_counters(trade_date, pnl_delta)
        self._trades_today += 1
        self._realized_pnl_today += pnl_delta

        if self._realized_pnl_today <= -abs(self.cfg.max_daily_loss_inr):
            reason = (
                f"Daily loss limit breached: {self._realized_pnl_today:.2f} "
                f"<= -{self.cfg.max_daily_loss_inr}"
            )
            self._halt(reason, "AUTO")

    def manual_halt(self, reason: str = "manual kill switch"):
        self._halt(reason, "MANUAL", log_prefix="TRADING HALTED (manual)")

    def halt_circuit_breaker(self, consecutive_failures: int):
        reason = f"Circuit breaker: {consecutive_failures} consecutive cycle failures."
        self._halt(reason, "AUTO", log_prefix="TRADING HALTED (circuit breaker)")

    def halt_reconciliation_mismatch(self, reason: str):
        self._halt(reason, "AUTO", log_prefix="TRADING HALTED (reconciliation mismatch)")

    def resume(self, reason: str = "manual resume"):
        if not self.halted:
            logger.warning("resume() called but not currently halted — no-op.")
            return
        if self.halt_source != "MANUAL":
            raise RuntimeError(
                f"Refusing to resume: current halt was automatic ({self.halt_reason!r}). "
                "Automatic halts (e.g. daily loss limit) can only clear via the next "
                "trading day's rollover, not a manual resume — this is intentional."
            )
        self.halted = False
        self.halt_reason = ""
        self.halt_source = "AUTO"
        risk_repo.set_halted(self._current_day.isoformat(), False, "", source="AUTO")
        risk_repo.record_risk_event(
            order_id=None, symbol=None, event_type="HALT_CLEARED", reasons=[reason],
        )
        logger.warning("TRADING RESUMED (manual): %s", reason)
        self._notify(f"🟢 TRADING RESUMED (manual): {reason}")

    def check(self, order: ProposedOrder, last_traded_price: float | None,
              order_id: int) -> RiskCheckResult:
        self._roll_day_if_needed()
        self.refresh_halt_state()
        reasons = []

        if self.halted:
            result = RiskCheckResult(approved=False, reasons=[f"Trading halted: {self.halt_reason}"])
            self._record_check_event(order_id, order, result, last_traded_price)
            return result

        if order.segment.value == "FNO" and not self.cfg.allow_fno:
            reasons.append(
                "F&O orders are disabled (set ALLOW_FNO=true in .env to enable — "
                "F&O uses leverage and carries materially higher risk per lot)."
            )

        if order.qty <= 0:
            reasons.append("Quantity must be positive.")

        # Entry-sizing gate — only applies to new exposure (BUY). A SELL closing a real
        # position must never be blocked by it: qty is fixed at whatever was actually
        # bought, and refusing to let a position close because it's "too big" would trap
        # it open indefinitely (found live via a nightly-optimize crash: a SELL rejected
        # here left the strategy's internal state believing it was flat while the real
        # position stayed OPEN in the DB, so the next BUY signal collided with it).
        if order.side == Side.BUY and order.total_units > self.cfg.max_position_qty:
            reasons.append(
                f"Total units {order.total_units} (qty {order.qty} x lot {order.lot_size}) "
                f"exceeds max_position_qty {self.cfg.max_position_qty}."
            )

        if self.mode != "PAPER" and self._trades_today >= self.cfg.max_trades_per_day:
            reasons.append(
                f"Daily trade count limit reached ({self.cfg.max_trades_per_day})."
            )

        margin_quote = None
        ref_price = order.limit_price or last_traded_price
        if ref_price is None:
            reasons.append("No reference price available to value the order — rejecting.")
        else:
            if order.segment == Segment.FNO and self._margin_provider is not None:
                margin_quote = self._check_fno_margin(order, reasons)
            elif order.segment == Segment.FNO and self.mode != "PAPER":
                # LIVE FNO with no margin_provider configured: refuse to size on notional
                # value alone rather than silently falling back to the CASH-style
                # price*qty math, which materially understates real F&O risk.
                reasons.append(
                    "F&O margin check unavailable (no margin_provider configured) — "
                    "refusing to size an FNO order on notional value alone in LIVE mode."
                )
            else:
                # CASH (any mode), or PAPER-mode FNO with no margin_provider configured yet
                # — falls back to the existing notional-value math so Phase B strategy
                # development/testing doesn't require the live margin API wired in.
                self._check_notional_value(order, ref_price, reasons)

            if (
                order.order_type.value == "LIMIT"
                and last_traded_price
                and order.limit_price
            ):
                band = self.cfg.price_sanity_band_pct / 100.0
                lo, hi = last_traded_price * (1 - band), last_traded_price * (1 + band)
                if not (lo <= order.limit_price <= hi):
                    reasons.append(
                        f"Limit price {order.limit_price} outside sanity band "
                        f"[{lo:.2f}, {hi:.2f}] around LTP {last_traded_price}."
                    )

        approved = len(reasons) == 0
        result = RiskCheckResult(approved=approved, reasons=reasons, margin_quote=margin_quote)
        self._record_check_event(order_id, order, result, last_traded_price)
        if not approved:
            logger.warning("Order REJECTED by risk manager: %s | order=%s", reasons, order)
        return result

    def _check_notional_value(self, order: ProposedOrder, ref_price: float, reasons: list[str]):
        # Both checks here gate NEW exposure — BUY only. A SELL closes whatever qty is
        # actually held; its notional value can legitimately exceed the entry-time value
        # cap purely because price moved up since entry, and must never be blocked for
        # that reason (same "always allow closing a real position" principle as the
        # max_position_qty check in check() above — see its comment for the live incident
        # that surfaced this).
        if order.side != Side.BUY:
            return
        order_value = ref_price * order.total_units
        if order_value > self.cfg.max_order_value_inr:
            reasons.append(
                f"Order value ₹{order_value:.2f} exceeds cap ₹{self.cfg.max_order_value_inr}."
            )

        projected_capital = self._deployed_capital + order_value
        if projected_capital > self.cfg.total_capital_inr:
            reasons.append(
                f"Would deploy ₹{projected_capital:.2f} total, exceeding "
                f"total capital cap ₹{self.cfg.total_capital_inr:.2f} "
                f"(currently ₹{self._deployed_capital:.2f} deployed)."
            )

    def _check_fno_margin(self, order: ProposedOrder, reasons: list[str]):
        """Fails CLOSED on this order only, never halts the bot — a single flaky margin-API
        call means only this order is unsizeable this cycle; the next poll cycle retries.
        Halting the whole bot (including an unrelated CASH strategy) over one transient F&O
        margin-fetch hiccup would be disproportionate — unlike reconcile_positions's
        halt-on-mismatch, where a mismatch means the whole ledger can't be trusted.

        Returns the fetched MarginQuote (or None on fetch failure) so check() can attach it
        to the RiskCheckResult — lets the orchestrator record the real margin at fill time
        without a second API call."""
        quote = self._margin_provider.get_order_margin(order)
        if quote is None:
            reasons.append(
                "Could not fetch margin details for this order — rejecting (fail-closed; "
                "does not halt trading, only this order)."
            )
            return None

        if quote.required_margin > self.cfg.max_order_value_inr:
            reasons.append(
                f"Required margin ₹{quote.required_margin:.2f} exceeds cap "
                f"₹{self.cfg.max_order_value_inr}."
            )
        if quote.required_margin > quote.available_margin:
            reasons.append(
                f"Required margin ₹{quote.required_margin:.2f} exceeds available broker "
                f"margin ₹{quote.available_margin:.2f}."
            )
        return quote

    def _record_check_event(self, order_id: int, order: ProposedOrder,
                             result: RiskCheckResult, last_traded_price: float | None):
        ref_price = order.limit_price or last_traded_price
        order_value = (ref_price * order.total_units) if ref_price is not None else None
        risk_repo.record_risk_event(
            order_id=order_id,
            symbol=order.symbol,
            event_type="CHECK_APPROVED" if result.approved else "CHECK_REJECTED",
            reasons=result.reasons,
            reference_price=ref_price,
            order_value=order_value,
            deployed_capital=self._deployed_capital,
            trades_today=self._trades_today,
            realized_pnl_today=self._realized_pnl_today,
        )
