"""
Margin-aware F&O order sizing, sourced from Groww's own pre-trade margin-check API
rather than the naive price*qty math RiskManager otherwise uses (which is correct for
CASH but not for FNO — real F&O margin is SPAN + exposure margin, not notional value).

Injected into RiskManager as an optional dependency (mirroring how ntfy_topic/today_fn
are already injected) — see core/risk_manager.py's FNO branch in check().
"""
import logging

from core.execution import _groww_segment, _build_trading_symbol
from core.models import MarginQuote, ProposedOrder, Side

logger = logging.getLogger("groww_agent.margin_provider")


class GrowwMarginProvider:
    """Wraps LiveBroker/PaperBroker's underlying Groww client. Works against either —
    margin lookups are read-only and paper-safe, so PAPER mode can exercise the real
    margin API (via PaperBroker's market_data_client) without placing any real order."""

    def __init__(self, client):
        self._client = client

    def get_order_margin(self, order: ProposedOrder) -> MarginQuote | None:
        """Returns None on any fetch/parse failure — caller (RiskManager) decides what
        that means (reject the order, per the "fail-closed on this order, don't halt
        the bot" decision documented in risk_manager.check())."""
        try:
            trading_symbol = _build_trading_symbol(order)
            resp = self._client.get_order_margin_details(
                segment=_groww_segment(self._client, order.segment),
                orders=[{
                    "trading_symbol": trading_symbol,
                    "transaction_type": order.side.value,
                    "quantity": order.total_units,
                    "price": order.limit_price or 0,
                    "order_type": order.order_type.value,
                    "product": self._client.PRODUCT_NRML,
                    "exchange": self._client.EXCHANGE_NSE,
                }],
            )
            required_margin = resp["total_requirement"]

            available = self._client.get_available_margin_details()
            fno = available.get("fno_margin_details", {})
            is_future = order.option_type is None
            if is_future:
                available_margin = fno.get("future_balance_available", 0.0)
            elif order.side == Side.BUY:
                available_margin = fno.get("option_buy_balance_available", 0.0)
            else:
                available_margin = fno.get("option_sell_balance_available", 0.0)

            return MarginQuote(required_margin=required_margin, available_margin=available_margin)
        except Exception as e:
            logger.error("Margin fetch failed for %s %s: %s", order.side.value, order.symbol, e)
            return None
