"""
Execution layer. Two implementations sharing one interface:
  - PaperBroker: simulates fills against live LTP, no real orders.
  - LiveBroker: wraps the actual Groww order API.

Supports both CASH (equity) and FNO (futures & options) segments.
"""
import logging
import uuid
from abc import ABC, abstractmethod

from growwapi.groww.exceptions import GrowwAPIAuthenticationException, GrowwAPIAuthorisationException

from core.auth import get_client, GrowwAuthError
from core.models import ProposedOrder, ExecutionResult, Segment

logger = logging.getLogger("groww_agent.execution")


def _derive_order_reference_id(idempotency_key: str) -> str:
    """Groww's SDK defaults order_reference_id to a random 8-digit numeric string when none
    is given (growwapi/groww/client.py: str(random.randint(10000000, 99999999))) — the SDK
    itself has no client-side format validation, but nothing confirms the backend accepts
    other formats (e.g. a 36-char UUID) either, and that can only be confirmed against the
    real API (see scripts/live_order_smoketest.py, deliberately not run yet). Deterministically
    derives an 8-digit numeric ID from the order's own idempotency_key instead of a random one,
    so it matches the SDK's own observed convention exactly while still being unique per order
    and traceable back to the order that generated it."""
    return str(uuid.UUID(idempotency_key).int % 100_000_000).zfill(8)


class BrokerPositionFetchError(Exception):
    pass


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

    def _reauth(self) -> bool:
        try:
            self.client = get_client()
            logger.warning("[LIVE] Re-authentication succeeded — refreshed Groww client.")
            return True
        except GrowwAuthError as e:
            logger.error("[LIVE] Re-authentication FAILED: %s", e)
            return False

    def get_ltp(self, symbol: str, segment: Segment = Segment.CASH) -> float | None:
        def _call():
            return self.client.get_quote(
                exchange=self.client.EXCHANGE_NSE,
                segment=_groww_segment(self.client, segment),
                trading_symbol=symbol,
            )

        try:
            quote = _call()
        except (GrowwAPIAuthenticationException, GrowwAPIAuthorisationException) as e:
            logger.error("LTP fetch auth error for %s (%s): %s — attempting re-auth.",
                         symbol, segment.value, e)
            if not self._reauth():
                return None
            try:
                quote = _call()
            except Exception as e2:
                logger.error("LTP fetch FAILED after re-auth retry for %s (%s): %s",
                             symbol, segment.value, e2)
                return None
        except Exception as e:
            logger.error("LTP fetch failed for %s (%s): %s", symbol, segment.value, e)
            return None

        return quote.get("last_price") or quote.get("ltp")

    def get_broker_position(self, symbol: str, segment: Segment = Segment.CASH) -> dict:
        """Returns {"symbol": symbol, "qty": <int>} — qty=0 means broker confirms flat.
        Raises BrokerPositionFetchError (never returns None) on any fetch/auth failure, so
        callers can't mistake 'fetch failed' for 'confirmed flat'."""
        def _call():
            return self.client.get_position_for_trading_symbol(
                trading_symbol=symbol, segment=_groww_segment(self.client, segment),
            )

        try:
            response = _call()
        except (GrowwAPIAuthenticationException, GrowwAPIAuthorisationException) as e:
            logger.error("[LIVE] Position fetch auth error for %s: %s — attempting re-auth.", symbol, e)
            if not self._reauth():
                raise BrokerPositionFetchError(
                    f"Re-authentication failed fetching position for {symbol}"
                ) from e
            try:
                response = _call()
            except Exception as e2:
                raise BrokerPositionFetchError(
                    f"Position fetch failed after re-auth retry for {symbol}: {e2}"
                ) from e2
        except Exception as e:
            raise BrokerPositionFetchError(f"Position fetch failed for {symbol}: {e}") from e

        qty = (response or {}).get("quantity") or (response or {}).get("qty") or 0
        return {"symbol": symbol, "qty": qty}

    def place_order(self, order: ProposedOrder, last_traded_price: float | None) -> ExecutionResult:
        trading_symbol = _build_trading_symbol(order)
        order_reference_id = _derive_order_reference_id(order.idempotency_key)
        logger.info(
            "[LIVE] Submitting order: %s %s qty=%s lot=%s total_units=%s segment=%s type=%s "
            "limit=%s key=%s ref=%s reason=%s",
            order.side.value, trading_symbol, order.qty, order.lot_size, order.total_units,
            order.segment.value, order.order_type.value, order.limit_price,
            order.idempotency_key, order_reference_id, order.reason,
        )

        def _call():
            return self.client.place_order(
                trading_symbol=trading_symbol,
                quantity=order.total_units,
                transaction_type=order.side.value,
                order_type=order.order_type.value,
                segment=_groww_segment(self.client, order.segment),
                exchange=self.client.EXCHANGE_NSE,
                product=_groww_product(self.client, order.segment),
                validity=self.client.VALIDITY_DAY,
                price=order.limit_price,
                order_reference_id=order_reference_id,
            )

        try:
            response = _call()
        except (GrowwAPIAuthenticationException, GrowwAPIAuthorisationException) as e:
            logger.error("[LIVE] Order placement failed due to auth error: %s — attempting re-auth.", e)
            if not self._reauth():
                return ExecutionResult(
                    order=order, status="ERROR",
                    message=f"Re-authentication failed after auth error: {e}",
                )
            try:
                response = _call()
            except Exception as e2:
                logger.error("[LIVE] Order placement FAILED after re-auth retry: %s | order=%s "
                             "ref=%s — check get_order_status_by_reference before retrying, a "
                             "timeout here does not prove the order was never received.",
                             e2, order, order_reference_id)
                return ExecutionResult(
                    order=order, status="ERROR", message=f"Auth retry failed: {e2}",
                )
        except Exception as e:
            logger.error("[LIVE] Order placement FAILED: %s | order=%s ref=%s — check "
                         "get_order_status_by_reference before retrying, a timeout here does "
                         "not prove the order was never received.", e, order, order_reference_id)
            return ExecutionResult(order=order, status="ERROR", message=str(e))

        broker_order_id = response.get("groww_order_id") or response.get("order_id")
        status = response.get("order_status", "PENDING")
        logger.info("[LIVE] Order response: id=%s status=%s", broker_order_id, status)
        return ExecutionResult(
            order=order,
            status=status,
            broker_order_id=broker_order_id,
            message=str(response),
        )

    def get_order_status_by_reference(self, order_reference_id: str, segment: Segment = Segment.CASH) -> dict:
        """Resolves the "did it actually go through" ambiguity from a place_order() timeout —
        looks the order up by the same order_reference_id place_order() derived and sent, rather
        than by Groww's own broker_order_id (which a failed/timed-out call never received).
        Raises on fetch/auth failure, same contract as get_broker_position — a caller must not
        mistake "couldn't check" for "confirmed not placed"."""
        def _call():
            return self.client.get_order_status_by_reference(
                segment=_groww_segment(self.client, segment),
                order_reference_id=order_reference_id,
            )

        try:
            return _call()
        except (GrowwAPIAuthenticationException, GrowwAPIAuthorisationException) as e:
            logger.error("[LIVE] Order status lookup auth error for ref=%s: %s — attempting re-auth.",
                         order_reference_id, e)
            if not self._reauth():
                raise BrokerPositionFetchError(
                    f"Re-authentication failed looking up order ref={order_reference_id}"
                ) from e
            try:
                return _call()
            except Exception as e2:
                raise BrokerPositionFetchError(
                    f"Order status lookup failed after re-auth retry for ref={order_reference_id}: {e2}"
                ) from e2
        except Exception as e:
            raise BrokerPositionFetchError(
                f"Order status lookup failed for ref={order_reference_id}: {e}"
            ) from e

    def cancel_order(self, broker_order_id: str, segment: Segment = Segment.CASH) -> dict:
        """Used by scripts/cancel_order.py (operator-invoked, not called from the trading
        loop). Raises on fetch/auth failure — same "never silently treat as success" contract
        as get_broker_position/get_order_status_by_reference."""
        def _call():
            return self.client.cancel_order(
                groww_order_id=broker_order_id,
                segment=_groww_segment(self.client, segment),
            )

        try:
            return _call()
        except (GrowwAPIAuthenticationException, GrowwAPIAuthorisationException) as e:
            logger.error("[LIVE] Cancel order auth error for id=%s: %s — attempting re-auth.",
                         broker_order_id, e)
            if not self._reauth():
                raise BrokerPositionFetchError(
                    f"Re-authentication failed cancelling order {broker_order_id}"
                ) from e
            try:
                return _call()
            except Exception as e2:
                raise BrokerPositionFetchError(
                    f"Cancel failed after re-auth retry for order {broker_order_id}: {e2}"
                ) from e2
        except Exception as e:
            raise BrokerPositionFetchError(f"Cancel failed for order {broker_order_id}: {e}") from e
