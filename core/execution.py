"""
Execution layer. Two implementations sharing one interface:
  - PaperBroker: simulates fills against live LTP, no real orders.
  - LiveBroker: wraps the actual Groww order API.

Supports both CASH (equity) and FNO (futures & options) segments.
"""
import logging
from abc import ABC, abstractmethod

from core.models import ProposedOrder, ExecutionResult, Segment

logger = logging.getLogger("groww_agent.execution")


def _groww_segment(client, segment: Segment):
    return client.SEGMENT_FNO if segment == Segment.FNO else client.SEGMENT_CASH


def _groww_product(client, segment: Segment):
    # FNO product selection is a placeholder — ALLOW_FNO is off by default and not in
    # active use; revisit (PRODUCT_NRML vs PRODUCT_MIS) if F&O is enabled.
    return client.PRODUCT_MIS


def _build_trading_symbol(order: ProposedOrder) -> str:
    if order.segment == Segment.CASH:
        return order.symbol

    parts = [order.symbol]
    if order.expiry_date is not None:
        parts.append(order.expiry_date.strftime("%y%b").upper())
    if order.strike_price is not None and order.option_type is not None:
        parts.append(str(int(order.strike_price)))
        parts.append(order.option_type.value)
    else:
        parts.append("FUT")
    return "".join(parts)


class Broker(ABC):
    @abstractmethod
    def place_order(self, order: ProposedOrder, last_traded_price: float | None) -> ExecutionResult:
        ...

    @abstractmethod
    def get_ltp(self, symbol: str, segment: Segment = Segment.CASH) -> float | None:
        ...


class PaperBroker(Broker):
    def __init__(self, market_data_client=None):
        self._market_data_client = market_data_client
        self._fill_log: list[ExecutionResult] = []

    def get_ltp(self, symbol: str, segment: Segment = Segment.CASH) -> float | None:
        if self._market_data_client is None:
            return None
        try:
            quote = self._market_data_client.get_quote(
                exchange=self._market_data_client.EXCHANGE_NSE,
                segment=_groww_segment(self._market_data_client, segment),
                trading_symbol=symbol,
            )
            return quote.get("last_price") or quote.get("ltp")
        except Exception as e:
            logger.error("Paper broker LTP fetch failed for %s (%s): %s", symbol, segment.value, e)
            return None

    def place_order(self, order: ProposedOrder, last_traded_price: float | None) -> ExecutionResult:
        fill_price = order.limit_price or last_traded_price
        if fill_price is None:
            return ExecutionResult(
                order=order, status="ERROR", message="No price available to simulate fill."
            )
        result = ExecutionResult(
            order=order,
            status="FILLED",
            broker_order_id=f"PAPER-{order.idempotency_key[:8]}",
            fill_price=fill_price,
            message=f"Simulated fill (paper trading, {order.segment.value}).",
        )
        self._fill_log.append(result)
        logger.info("[PAPER] %s %s x%s (%s, lot=%s) @ %.2f — %s",
                    order.side.value, order.symbol, order.qty, order.segment.value,
                    order.lot_size, fill_price, order.reason)
        return result


class LiveBroker(Broker):
    def __init__(self, groww_client):
        self.client = groww_client

    def get_ltp(self, symbol: str, segment: Segment = Segment.CASH) -> float | None:
        try:
            quote = self.client.get_quote(
                exchange=self.client.EXCHANGE_NSE,
                segment=_groww_segment(self.client, segment),
                trading_symbol=symbol,
            )
            return quote.get("last_price") or quote.get("ltp")
        except Exception as e:
            logger.error("LTP fetch failed for %s (%s): %s", symbol, segment.value, e)
            return None

    def place_order(self, order: ProposedOrder, last_traded_price: float | None) -> ExecutionResult:
        trading_symbol = _build_trading_symbol(order)
        logger.info(
            "[LIVE] Submitting order: %s %s qty=%s lot=%s total_units=%s segment=%s type=%s "
            "limit=%s key=%s reason=%s",
            order.side.value, trading_symbol, order.qty, order.lot_size, order.total_units,
            order.segment.value, order.order_type.value, order.limit_price,
            order.idempotency_key, order.reason,
        )
        try:
            # TODO: consider passing order_reference_id=order.idempotency_key once it's
            # confirmed Groww's API accepts a 36-char UUID (docstring implies an 8-digit
            # numeric default; unverified whether a longer string is rejected).
            response = self.client.place_order(
                trading_symbol=trading_symbol,
                quantity=order.total_units,
                transaction_type=order.side.value,
                order_type=order.order_type.value,
                segment=_groww_segment(self.client, order.segment),
                exchange=self.client.EXCHANGE_NSE,
                product=_groww_product(self.client, order.segment),
                validity=self.client.VALIDITY_DAY,
                price=order.limit_price,
            )
            broker_order_id = response.get("groww_order_id") or response.get("order_id")
            status = response.get("order_status", "PENDING")
            logger.info("[LIVE] Order response: id=%s status=%s", broker_order_id, status)
            return ExecutionResult(
                order=order,
                status=status,
                broker_order_id=broker_order_id,
                message=str(response),
            )
        except Exception as e:
            logger.error("[LIVE] Order placement FAILED: %s | order=%s", e, order)
            return ExecutionResult(order=order, status="ERROR", message=str(e))
