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

from config.settings import RiskConfig
from core.db import positions_repo, risk_repo
from core.models import ProposedOrder, RiskCheckResult, Side
from core.notifier import send_telegram_raw

logger = logging.getLogger("groww_agent.risk")


class RiskManager:
    def __init__(self, risk_config: RiskConfig, telegram_bot_token: str = "",
                 telegram_chat_id: str = ""):
        self.cfg = risk_config
        self._telegram_bot_token = telegram_bot_token
        self._telegram_chat_id = telegram_chat_id
        self._current_day = date.today()
        self._sync_daily_state()

    def _notify(self, message: str):
        try:
            send_telegram_raw(self._telegram_bot_token, self._telegram_chat_id, message)
        except Exception as e:
            logger.error("Failed to send notification: %s", e)

    def _sync_daily_state(self):
        summary = risk_repo.get_or_create_daily_summary(self._current_day.isoformat())
        self._trades_today = summary["trades_count"]
        self._realized_pnl_today = summary["realized_pnl"]
        self.halted = bool(summary["halted"])
        self.halt_reason = summary["halt_reason"]

    def _roll_day_if_needed(self):
        today = date.today()
        if today != self._current_day:
            self._current_day = today
            self._sync_daily_state()
            logger.info("New trading day — risk counters reset.")

    @property
    def _deployed_capital(self) -> float:
        return positions_repo.get_deployed_capital()

    def record_fill(self, side: Side, order_value: float, pnl_delta: float = 0.0,
                     order_id: int | None = None):
        trade_date = self._current_day.isoformat()
        risk_repo.increment_daily_counters(trade_date, pnl_delta)
        self._trades_today += 1
        self._realized_pnl_today += pnl_delta

        if self._realized_pnl_today <= -abs(self.cfg.max_daily_loss_inr):
            self.halted = True
            self.halt_reason = (
                f"Daily loss limit breached: {self._realized_pnl_today:.2f} "
                f"<= -{self.cfg.max_daily_loss_inr}"
            )
            risk_repo.set_halted(trade_date, True, self.halt_reason)
            risk_repo.record_risk_event(
                order_id=order_id, symbol=None, event_type="HALTED", reasons=[self.halt_reason],
            )
            logger.critical("TRADING HALTED: %s", self.halt_reason)
            self._notify(f"🔴 TRADING HALTED: {self.halt_reason}")

    def manual_halt(self, reason: str = "manual kill switch"):
        self.halted = True
        self.halt_reason = reason
        risk_repo.set_halted(self._current_day.isoformat(), True, reason)
        risk_repo.record_risk_event(
            order_id=None, symbol=None, event_type="HALTED", reasons=[reason],
        )
        logger.critical("TRADING HALTED (manual): %s", reason)
        self._notify(f"🔴 TRADING HALTED (manual): {reason}")

    def check(self, order: ProposedOrder, last_traded_price: float | None,
              order_id: int) -> RiskCheckResult:
        self._roll_day_if_needed()
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

        if order.total_units > self.cfg.max_position_qty:
            reasons.append(
                f"Total units {order.total_units} (qty {order.qty} x lot {order.lot_size}) "
                f"exceeds max_position_qty {self.cfg.max_position_qty}."
            )

        if self._trades_today >= self.cfg.max_trades_per_day:
            reasons.append(
                f"Daily trade count limit reached ({self.cfg.max_trades_per_day})."
            )

        ref_price = order.limit_price or last_traded_price
        if ref_price is None:
            reasons.append("No reference price available to value the order — rejecting.")
        else:
            order_value = ref_price * order.total_units
            if order_value > self.cfg.max_order_value_inr:
                reasons.append(
                    f"Order value ₹{order_value:.2f} exceeds cap ₹{self.cfg.max_order_value_inr}."
                )

            if order.side == Side.BUY:
                projected_capital = self._deployed_capital + order_value
                if projected_capital > self.cfg.total_capital_inr:
                    reasons.append(
                        f"Would deploy ₹{projected_capital:.2f} total, exceeding "
                        f"total capital cap ₹{self.cfg.total_capital_inr:.2f} "
                        f"(currently ₹{self._deployed_capital:.2f} deployed)."
                    )

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
        result = RiskCheckResult(approved=approved, reasons=reasons)
        self._record_check_event(order_id, order, result, last_traded_price)
        if not approved:
            logger.warning("Order REJECTED by risk manager: %s | order=%s", reasons, order)
        return result

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
