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
from core.status_writer import write_heartbeat

logger = logging.getLogger("groww_agent.orchestrator")


class Orchestrator:
    def __init__(self, settings: Settings, broker, risk_manager, strategy):
        self.settings = settings
        self.broker = broker
        self.risk_manager = risk_manager
        self.strategy = strategy
        self._last_ltp: dict[str, float | None] = {}

    def run_once(self, symbols: list[str]):
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

    def run_forever(self, symbols: list[str], poll_interval_sec: int = 5):
        logger.info("Starting orchestrator loop | mode=%s | symbols=%s",
                    self.settings.mode, symbols)
        while True:
            try:
                self.run_once(symbols)
            except Exception as e:
                logger.exception("Unhandled error in run_once: %s", e)
            time.sleep(poll_interval_sec)
