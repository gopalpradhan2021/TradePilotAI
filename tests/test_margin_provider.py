from core.execution import GROWW_API_TIMEOUT_SEC
from core.margin_provider import GrowwMarginProvider
from core.models import OptionType, OrderType, ProposedOrder, Segment, Side


def make_order(**overrides):
    defaults = dict(
        symbol="NIFTY2690122000CE", side=Side.BUY, qty=1, order_type=OrderType.MARKET,
        segment=Segment.FNO, lot_size=75, option_type=OptionType.CE,
    )
    defaults.update(overrides)
    return ProposedOrder(**defaults)


class FakeGrowwClient:
    EXCHANGE_NSE = "NSE"
    SEGMENT_CASH = "CASH"
    SEGMENT_FNO = "FNO"
    PRODUCT_NRML = "NRML"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    ORDER_TYPE_MARKET = "MARKET"

    def __init__(self, order_margin_response=None, order_margin_raise_exc=None,
                 available_margin_response=None, available_margin_raise_exc=None):
        self.order_margin_calls = []
        self.available_margin_calls = []
        self._order_margin_response = order_margin_response if order_margin_response is not None else {
            "total_requirement": 470.32,
        }
        self._order_margin_raise_exc = order_margin_raise_exc
        self._available_margin_response = available_margin_response if available_margin_response is not None else {
            "fno_margin_details": {
                "future_balance_available": 1000.0,
                "option_buy_balance_available": 2000.0,
                "option_sell_balance_available": 3000.0,
            },
        }
        self._available_margin_raise_exc = available_margin_raise_exc

    def get_order_margin_details(self, **kwargs):
        self.order_margin_calls.append(kwargs)
        if self._order_margin_raise_exc is not None:
            raise self._order_margin_raise_exc
        return self._order_margin_response

    def get_available_margin_details(self, **kwargs):
        self.available_margin_calls.append(kwargs)
        if self._available_margin_raise_exc is not None:
            raise self._available_margin_raise_exc
        return self._available_margin_response


def test_get_order_margin_returns_quote_for_option_buy():
    client = FakeGrowwClient()
    provider = GrowwMarginProvider(client)
    order = make_order(side=Side.BUY)

    quote = provider.get_order_margin(order)

    assert quote is not None
    assert quote.required_margin == 470.32
    assert quote.available_margin == 2000.0  # option_buy_balance_available


def test_get_order_margin_uses_option_sell_balance_for_sell_orders():
    client = FakeGrowwClient()
    provider = GrowwMarginProvider(client)
    order = make_order(side=Side.SELL)

    quote = provider.get_order_margin(order)

    assert quote.available_margin == 3000.0  # option_sell_balance_available


def test_get_order_margin_uses_future_balance_for_futures():
    client = FakeGrowwClient()
    provider = GrowwMarginProvider(client)
    order = make_order(option_type=None, symbol="NIFTY26SEPFUT")  # no option_type => future

    quote = provider.get_order_margin(order)

    assert quote.available_margin == 1000.0  # future_balance_available


def test_get_order_margin_sends_required_sdk_fields():
    client = FakeGrowwClient()
    provider = GrowwMarginProvider(client)
    order = make_order(qty=2, lot_size=75)

    provider.get_order_margin(order)

    assert len(client.order_margin_calls) == 1
    sent_order = client.order_margin_calls[0]["orders"][0]
    assert sent_order["trading_symbol"] == "NIFTY2690122000CE"
    assert sent_order["transaction_type"] == "BUY"
    assert sent_order["quantity"] == 150  # qty(2) * lot_size(75)
    assert sent_order["exchange"] == client.EXCHANGE_NSE
    assert sent_order["product"] == client.PRODUCT_NRML


def test_get_order_margin_returns_none_on_order_margin_fetch_failure():
    client = FakeGrowwClient(order_margin_raise_exc=RuntimeError("network down"))
    provider = GrowwMarginProvider(client)

    assert provider.get_order_margin(make_order()) is None


def test_get_order_margin_returns_none_on_available_margin_fetch_failure():
    client = FakeGrowwClient(available_margin_raise_exc=RuntimeError("network down"))
    provider = GrowwMarginProvider(client)

    assert provider.get_order_margin(make_order()) is None


def test_get_order_margin_returns_none_on_missing_total_requirement_key():
    client = FakeGrowwClient(order_margin_response={"some_other_key": 1.0})
    provider = GrowwMarginProvider(client)

    assert provider.get_order_margin(make_order()) is None


def test_get_order_margin_passes_timeout_on_both_calls():
    # growwapi defaults to timeout=None (infinite) — a hung margin call would freeze the
    # bot's risk-check path exactly like the live 3-hour incident this fixes (2026-08-31).
    client = FakeGrowwClient()
    provider = GrowwMarginProvider(client)

    provider.get_order_margin(make_order())

    assert client.order_margin_calls[0]["timeout"] == GROWW_API_TIMEOUT_SEC
    assert client.available_margin_calls[0]["timeout"] == GROWW_API_TIMEOUT_SEC
