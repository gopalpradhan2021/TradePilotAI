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
from core.models import ProposedOrder, Side
from core.notifier import send_notification
from core.status_writer import write_heartbeat

logger = logging.getLogger("groww_agent.orchestrator")


class Orchestrator:
    _CIRCUIT_BREAKER_THRESHOLD = 5  # ~25s of continuous failure at the default 5s poll interval

    def __init__(self, settings: Settings, broker, risk_manager, strategy):
        self.settings = settings
        self.broker = broker
        self.risk_manager = risk_manager
        self.strategy = strategy
        self._last_ltp: dict[str, float | None] = {}
        self._consecutive_failures = 0

    def _notify(self, message: str):
        try:
            send_notification(self.settings, message)
        except Exception as e:
            logger.error("Failed to send notification: %s", e)

    def run_once(self, symbols: list[str]):
        self.risk_manager.refresh_halt_state()
        if self.risk_manager.halted:
            logger.warning("Skipping cycle — risk manager is halted: %s",
                            self.risk_manager.halt_reason)
            self._write_heartbeat(symbols)
            return

        for symbol in symbols:
            ltp = self.broker.get_ltp(symbol)
            self._last_ltp[symbol] = ltp
            proposed = self.strategy.decide(symbol=symbol, last_traded_price=ltp)
            if proposed is None:
                continue

            self._handle_proposed_order(proposed, ltp)

        self._write_heartbeat(symbols)

    def _write_heartbeat(self, symbols: list[str]):
        try:
            write_heartbeat(
                mode=self.settings.mode,
                halted=self.risk_manager.halted,
                halt_reason=self.risk_manager.halt_reason,
                symbols=symbols,
                last_ltp=self._last_ltp,
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
        orders_repo.update_order_status(
            order_id,
            status=result.status,
            broker_order_id=result.broker_order_id,
            fill_price=result.fill_price,
            message=result.message,
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

    def _run_cycle(self, symbols: list[str]):
        try:
            self.run_once(symbols)
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

    def run_forever(self, symbols: list[str], poll_interval_sec: int = 5):
        logger.info("Starting orchestrator loop | mode=%s | symbols=%s",
                    self.settings.mode, symbols)
        while True:
            self._run_cycle(symbols)
            time.sleep(poll_interval_sec)
