from datetime import date

from core.execution import PaperBroker, LiveBroker, _build_trading_symbol
from core.models import ProposedOrder, Side, OrderType, Segment, OptionType


def make_order(**overrides):
    defaults = dict(symbol="RELIANCE", side=Side.BUY, qty=1, order_type=OrderType.MARKET)
    defaults.update(overrides)
    return ProposedOrder(**defaults)


class FakeGrowwClient:
    EXCHANGE_NSE = "NSE"
    SEGMENT_CASH = "CASH"
    SEGMENT_FNO = "FNO"
    PRODUCT_MIS = "MIS"
    VALIDITY_DAY = "DAY"

    def __init__(self, response=None, raise_exc=None):
        self.calls = []
        self._response = response if response is not None else {
            "groww_order_id": "ORD123", "order_status": "PENDING",
        }
        self._raise_exc = raise_exc

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


def test_paper_broker_fills_at_ltp_when_no_limit_price():
    broker = PaperBroker()
    result = broker.place_order(make_order(), last_traded_price=150.0)
    assert result.status == "FILLED"
    assert result.fill_price == 150.0


def test_paper_broker_fills_at_limit_price_when_set():
    broker = PaperBroker()
    order = make_order(order_type=OrderType.LIMIT, limit_price=145.0)
    result = broker.place_order(order, last_traded_price=150.0)
    assert result.fill_price == 145.0


def test_paper_broker_errors_when_no_price_available():
    broker = PaperBroker()
    result = broker.place_order(make_order(), last_traded_price=None)
    assert result.status == "ERROR"


def test_paper_broker_get_ltp_without_client_returns_none():
    broker = PaperBroker(market_data_client=None)
    assert broker.get_ltp("RELIANCE") is None


def test_build_trading_symbol_cash_segment_is_plain_symbol():
    order = make_order(segment=Segment.CASH)
    assert _build_trading_symbol(order) == "RELIANCE"


def test_build_trading_symbol_fno_option():
    order = make_order(
        symbol="NIFTY", segment=Segment.FNO, lot_size=50,
        expiry_date=date(2026, 9, 25), strike_price=25000.0, option_type=OptionType.CE,
    )
    assert _build_trading_symbol(order) == "NIFTY26SEP25000CE"


def test_build_trading_symbol_fno_future_without_strike():
    order = make_order(
        symbol="NIFTY", segment=Segment.FNO, lot_size=50,
        expiry_date=date(2026, 9, 25),
    )
    assert _build_trading_symbol(order) == "NIFTY26SEPFUT"


def test_live_broker_place_order_passes_required_sdk_fields():
    client = FakeGrowwClient()
    broker = LiveBroker(client)
    broker.place_order(make_order(segment=Segment.CASH), last_traded_price=150.0)

    assert len(client.calls) == 1
    kwargs = client.calls[0]
    assert kwargs["exchange"] == client.EXCHANGE_NSE
    assert kwargs["product"] == client.PRODUCT_MIS
    assert kwargs["validity"] == client.VALIDITY_DAY
    assert kwargs["segment"] == client.SEGMENT_CASH


def test_live_broker_place_order_uses_total_units_not_qty():
    client = FakeGrowwClient()
    broker = LiveBroker(client)
    order = make_order(symbol="NIFTY", segment=Segment.FNO, qty=2, lot_size=50)
    broker.place_order(order, last_traded_price=100.0)

    assert client.calls[0]["quantity"] == 100  # qty(2) * lot_size(50)


def test_live_broker_place_order_cash_quantity_unchanged():
    client = FakeGrowwClient()
    broker = LiveBroker(client)
    order = make_order(segment=Segment.CASH, qty=3)
    broker.place_order(order, last_traded_price=100.0)

    assert client.calls[0]["quantity"] == 3


def test_live_broker_place_order_success_maps_response():
    client = FakeGrowwClient(response={"groww_order_id": "ORD999", "order_status": "PENDING"})
    broker = LiveBroker(client)
    result = broker.place_order(make_order(), last_traded_price=150.0)

    assert result.status == "PENDING"
    assert result.broker_order_id == "ORD999"


def test_live_broker_place_order_exception_returns_error_result():
    client = FakeGrowwClient(raise_exc=RuntimeError("network down"))
    broker = LiveBroker(client)
    result = broker.place_order(make_order(), last_traded_price=150.0)

    assert result.status == "ERROR"
