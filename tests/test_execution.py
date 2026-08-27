from datetime import date

import core.execution as execution_module
from core.auth import GrowwAuthError
from core.execution import (
    PaperBroker, LiveBroker, BrokerPositionFetchError, _build_trading_symbol,
    _derive_order_reference_id,
)
from core.models import ProposedOrder, Side, OrderType, Segment, OptionType
from growwapi.groww.exceptions import GrowwAPIAuthenticationException


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

    def __init__(self, response=None, raise_exc=None,
                 quote_response=None, quote_raise_exc=None,
                 position_response=None, position_raise_exc=None,
                 order_status_response=None, order_status_raise_exc=None,
                 cancel_response=None, cancel_raise_exc=None):
        self.calls = []
        self.quote_calls = []
        self.position_calls = []
        self.order_status_calls = []
        self.cancel_calls = []
        self._response = response if response is not None else {
            "groww_order_id": "ORD123", "order_status": "PENDING",
        }
        self._raise_exc = raise_exc
        self._quote_response = quote_response if quote_response is not None else {"last_price": 100.0}
        self._quote_raise_exc = quote_raise_exc
        self._position_response = position_response if position_response is not None else {"quantity": 0}
        self._position_raise_exc = position_raise_exc
        self._order_status_response = order_status_response if order_status_response is not None else {}
        self._order_status_raise_exc = order_status_raise_exc
        self._cancel_response = cancel_response if cancel_response is not None else {"order_status": "CANCELLED"}
        self._cancel_raise_exc = cancel_raise_exc

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response

    def get_quote(self, **kwargs):
        self.quote_calls.append(kwargs)
        if self._quote_raise_exc is not None:
            raise self._quote_raise_exc
        return self._quote_response

    def get_position_for_trading_symbol(self, **kwargs):
        self.position_calls.append(kwargs)
        if self._position_raise_exc is not None:
            raise self._position_raise_exc
        return self._position_response

    def get_order_status_by_reference(self, **kwargs):
        self.order_status_calls.append(kwargs)
        if self._order_status_raise_exc is not None:
            raise self._order_status_raise_exc
        return self._order_status_response

    def cancel_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        if self._cancel_raise_exc is not None:
            raise self._cancel_raise_exc
        return self._cancel_response


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


def test_live_broker_reauth_and_retries_once_on_auth_exception(monkeypatch):
    failing_client = FakeGrowwClient(raise_exc=GrowwAPIAuthenticationException())
    new_client = FakeGrowwClient(response={"groww_order_id": "NEW1", "order_status": "FILLED"})
    monkeypatch.setattr(execution_module, "get_client", lambda: new_client)

    broker = LiveBroker(failing_client)
    result = broker.place_order(make_order(), last_traded_price=150.0)

    assert result.status == "FILLED"
    assert result.broker_order_id == "NEW1"
    assert broker.client is new_client
    assert len(failing_client.calls) == 1
    assert len(new_client.calls) == 1


def test_live_broker_reauth_failure_returns_error(monkeypatch):
    failing_client = FakeGrowwClient(raise_exc=GrowwAPIAuthenticationException())

    def raise_auth_error():
        raise GrowwAuthError("cannot get client")

    monkeypatch.setattr(execution_module, "get_client", raise_auth_error)

    broker = LiveBroker(failing_client)
    result = broker.place_order(make_order(), last_traded_price=150.0)

    assert result.status == "ERROR"
    assert len(failing_client.calls) == 1  # no retry loop


def test_live_broker_get_ltp_reauths_on_auth_exception(monkeypatch):
    failing_client = FakeGrowwClient(quote_raise_exc=GrowwAPIAuthenticationException())
    new_client = FakeGrowwClient(quote_response={"last_price": 175.5})
    monkeypatch.setattr(execution_module, "get_client", lambda: new_client)

    broker = LiveBroker(failing_client)
    result = broker.get_ltp("RELIANCE")

    assert result == 175.5
    assert broker.client is new_client


def test_get_broker_position_returns_qty_from_response():
    client = FakeGrowwClient(position_response={"quantity": 5})
    broker = LiveBroker(client)

    assert broker.get_broker_position("RELIANCE") == {"symbol": "RELIANCE", "qty": 5}


def test_get_broker_position_defaults_to_zero_when_absent():
    client = FakeGrowwClient(position_response={})
    broker = LiveBroker(client)

    assert broker.get_broker_position("RELIANCE")["qty"] == 0


def test_get_broker_position_raises_on_generic_error():
    client = FakeGrowwClient(position_raise_exc=RuntimeError("network down"))
    broker = LiveBroker(client)

    try:
        broker.get_broker_position("RELIANCE")
        assert False, "expected BrokerPositionFetchError"
    except BrokerPositionFetchError:
        pass


def test_get_broker_position_reauths_on_auth_exception(monkeypatch):
    failing_client = FakeGrowwClient(position_raise_exc=GrowwAPIAuthenticationException())
    new_client = FakeGrowwClient(position_response={"quantity": 3})
    monkeypatch.setattr(execution_module, "get_client", lambda: new_client)

    broker = LiveBroker(failing_client)
    result = broker.get_broker_position("RELIANCE")

    assert result == {"symbol": "RELIANCE", "qty": 3}
    assert broker.client is new_client


def test_get_broker_position_raises_when_reauth_fails(monkeypatch):
    failing_client = FakeGrowwClient(position_raise_exc=GrowwAPIAuthenticationException())

    def raise_auth_error():
        raise GrowwAuthError("cannot get client")

    monkeypatch.setattr(execution_module, "get_client", raise_auth_error)

    broker = LiveBroker(failing_client)
    try:
        broker.get_broker_position("RELIANCE")
        assert False, "expected BrokerPositionFetchError"
    except BrokerPositionFetchError:
        pass
    assert len(failing_client.position_calls) == 1


# --- order_reference_id (idempotency / unknown-order recovery) ---------

def test_derive_order_reference_id_is_eight_digits():
    order = make_order()
    ref = _derive_order_reference_id(order.idempotency_key)
    assert len(ref) == 8
    assert ref.isdigit()


def test_derive_order_reference_id_is_deterministic():
    order = make_order()
    assert (_derive_order_reference_id(order.idempotency_key)
            == _derive_order_reference_id(order.idempotency_key))


def test_derive_order_reference_id_differs_for_different_keys():
    a = make_order()
    b = make_order()
    assert _derive_order_reference_id(a.idempotency_key) != _derive_order_reference_id(b.idempotency_key)


def test_live_broker_place_order_passes_order_reference_id():
    client = FakeGrowwClient()
    broker = LiveBroker(client)
    order = make_order()
    broker.place_order(order, last_traded_price=150.0)

    expected_ref = _derive_order_reference_id(order.idempotency_key)
    assert client.calls[0]["order_reference_id"] == expected_ref


def test_get_order_status_by_reference_returns_response():
    client = FakeGrowwClient(order_status_response={"order_status": "COMPLETE"})
    broker = LiveBroker(client)

    result = broker.get_order_status_by_reference("12345678")

    assert result == {"order_status": "COMPLETE"}
    assert client.order_status_calls[0]["order_reference_id"] == "12345678"


def test_get_order_status_by_reference_raises_on_generic_error():
    client = FakeGrowwClient(order_status_raise_exc=RuntimeError("network down"))
    broker = LiveBroker(client)

    try:
        broker.get_order_status_by_reference("12345678")
        assert False, "expected BrokerPositionFetchError"
    except BrokerPositionFetchError:
        pass


def test_get_order_status_by_reference_reauths_on_auth_exception(monkeypatch):
    failing_client = FakeGrowwClient(order_status_raise_exc=GrowwAPIAuthenticationException())
    new_client = FakeGrowwClient(order_status_response={"order_status": "REJECTED"})
    monkeypatch.setattr(execution_module, "get_client", lambda: new_client)

    broker = LiveBroker(failing_client)
    result = broker.get_order_status_by_reference("12345678")

    assert result == {"order_status": "REJECTED"}
    assert broker.client is new_client


# --- cancel_order (emergency cancel/flatten tools) -----------------------

def test_cancel_order_returns_response():
    client = FakeGrowwClient(cancel_response={"order_status": "CANCELLED"})
    broker = LiveBroker(client)

    result = broker.cancel_order("ORD123")

    assert result == {"order_status": "CANCELLED"}
    assert client.cancel_calls[0]["groww_order_id"] == "ORD123"
    assert client.cancel_calls[0]["segment"] == client.SEGMENT_CASH


def test_cancel_order_raises_on_generic_error():
    client = FakeGrowwClient(cancel_raise_exc=RuntimeError("network down"))
    broker = LiveBroker(client)

    try:
        broker.cancel_order("ORD123")
        assert False, "expected BrokerPositionFetchError"
    except BrokerPositionFetchError:
        pass


def test_cancel_order_reauths_on_auth_exception(monkeypatch):
    failing_client = FakeGrowwClient(cancel_raise_exc=GrowwAPIAuthenticationException())
    new_client = FakeGrowwClient(cancel_response={"order_status": "CANCELLED"})
    monkeypatch.setattr(execution_module, "get_client", lambda: new_client)

    broker = LiveBroker(failing_client)
    result = broker.cancel_order("ORD123")

    assert result == {"order_status": "CANCELLED"}
    assert broker.client is new_client
