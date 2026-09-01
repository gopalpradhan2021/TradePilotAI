from datetime import date, datetime

import core.execution as execution_module
from core.auth import GrowwAuthError
from core.execution import (
    PaperBroker, LiveBroker, BrokerPositionFetchError, _build_trading_symbol,
    _derive_order_reference_id, _parse_candles,
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
    PRODUCT_NRML = "NRML"
    VALIDITY_DAY = "DAY"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    ORDER_TYPE_MARKET = "MARKET"

    def __init__(self, response=None, raise_exc=None,
                 quote_response=None, quote_raise_exc=None,
                 position_response=None, position_raise_exc=None,
                 order_status_response=None, order_status_raise_exc=None,
                 cancel_response=None, cancel_raise_exc=None,
                 expiries_response=None, expiries_raise_exc=None,
                 option_chain_response=None, option_chain_raise_exc=None,
                 order_margin_response=None, order_margin_raise_exc=None,
                 available_margin_response=None, available_margin_raise_exc=None,
                 instrument_response=None, instrument_raise_exc=None,
                 candles_response=None, candles_raise_exc=None):
        self.calls = []
        self.quote_calls = []
        self.position_calls = []
        self.order_status_calls = []
        self.cancel_calls = []
        self.expiries_calls = []
        self.option_chain_calls = []
        self.instrument_calls = []
        self.order_margin_calls = []
        self.available_margin_calls = []
        self.candles_calls = []
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
        self._expiries_response = expiries_response if expiries_response is not None else {"expiries": []}
        self._expiries_raise_exc = expiries_raise_exc
        self._option_chain_response = option_chain_response if option_chain_response is not None else {
            "underlying_ltp": 0.0, "strikes": {},
        }
        self._option_chain_raise_exc = option_chain_raise_exc
        self._order_margin_response = order_margin_response if order_margin_response is not None else {
            "total_requirement": 0.0,
        }
        self._order_margin_raise_exc = order_margin_raise_exc
        self._available_margin_response = available_margin_response if available_margin_response is not None else {
            "fno_margin_details": {
                "future_balance_available": 0.0,
                "option_buy_balance_available": 0.0,
                "option_sell_balance_available": 0.0,
            },
        }
        self._available_margin_raise_exc = available_margin_raise_exc
        self._instrument_response = instrument_response if instrument_response is not None else {
            "lot_size": "75",
        }
        self._instrument_raise_exc = instrument_raise_exc
        self._candles_response = candles_response if candles_response is not None else {"candles": []}
        self._candles_raise_exc = candles_raise_exc

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

    def get_expiries(self, **kwargs):
        self.expiries_calls.append(kwargs)
        if self._expiries_raise_exc is not None:
            raise self._expiries_raise_exc
        return self._expiries_response

    def get_option_chain(self, **kwargs):
        self.option_chain_calls.append(kwargs)
        if self._option_chain_raise_exc is not None:
            raise self._option_chain_raise_exc
        return self._option_chain_response

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

    def get_instrument_by_exchange_and_trading_symbol(self, **kwargs):
        self.instrument_calls.append(kwargs)
        if self._instrument_raise_exc is not None:
            raise self._instrument_raise_exc
        return self._instrument_response

    def get_historical_candles(self, **kwargs):
        self.candles_calls.append(kwargs)
        if self._candles_raise_exc is not None:
            raise self._candles_raise_exc
        return self._candles_response


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


def test_build_trading_symbol_fno_option_passes_symbol_through_verbatim():
    # order.symbol is already the real broker trading_symbol, sourced from
    # get_option_chain() at decision time — _build_trading_symbol() must not try to
    # reconstruct it. A prior version guessed a 3-letter-month format here that live
    # testing showed does not match what Groww's own API actually returns or accepts.
    order = make_order(
        symbol="NIFTY2690122000CE", segment=Segment.FNO, lot_size=75,
        expiry_date=date(2026, 9, 1), strike_price=22000.0, option_type=OptionType.CE,
        underlying_symbol="NIFTY",
    )
    assert _build_trading_symbol(order) == "NIFTY2690122000CE"


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


# --- get_expiries / get_option_chain / get_lot_size (Phase A: F&O infra) --

_REALISTIC_CHAIN_RESPONSE = {
    "underlying_ltp": 24092.8,
    "strikes": {
        "22000": {
            "CE": {
                "trading_symbol": "NIFTY2690122000CE", "ltp": 2187.1,
                "open_interest": 75, "volume": 0,
                "greeks": {"delta": 0.9975, "gamma": 0, "theta": -0.7534,
                           "vega": 0.1976, "rho": 2.4765, "iv": 31.4553},
            },
            "PE": {
                "trading_symbol": "NIFTY2690122000PE", "ltp": 0.6,
                "open_interest": 57357, "volume": 69547,
                "greeks": {"delta": -0.0025, "gamma": 0, "theta": -0.7534,
                           "vega": 0.1976, "rho": -0.0068, "iv": 31.4553},
            },
        },
        "22050": {
            "CE": {
                "trading_symbol": "NIFTY2690122050CE", "ltp": 2059.15,
                "open_interest": 0, "volume": 0,
                "greeks": {"delta": 0.9976, "gamma": 0, "theta": -0.6972,
                           "vega": 0.1886, "rho": 2.4825, "iv": 30.5038},
            },
            # deliberately no PE here — real responses can be one-sided at extreme strikes
        },
    },
}


def test_paper_broker_get_expiries_parses_iso_dates():
    client = FakeGrowwClient(expiries_response={"expiries": ["2026-09-01", "2026-09-08"]})
    broker = PaperBroker(market_data_client=client)

    result = broker.get_expiries("NIFTY")

    assert result == [date(2026, 9, 1), date(2026, 9, 8)]


def test_paper_broker_get_expiries_without_client_returns_none():
    broker = PaperBroker(market_data_client=None)
    assert broker.get_expiries("NIFTY") is None


def test_paper_broker_get_option_chain_parses_realistic_response():
    client = FakeGrowwClient(option_chain_response=_REALISTIC_CHAIN_RESPONSE)
    broker = PaperBroker(market_data_client=client)

    snapshot = broker.get_option_chain("NIFTY", date(2026, 9, 1))

    assert snapshot is not None
    assert snapshot.underlying == "NIFTY"
    assert snapshot.underlying_ltp == 24092.8
    assert snapshot.expiry_date == date(2026, 9, 1)
    assert [s.strike for s in snapshot.strikes] == [22000.0, 22050.0]  # sorted ascending

    first = snapshot.strikes[0]
    assert first.ce.trading_symbol == "NIFTY2690122000CE"
    assert first.ce.open_interest == 75
    assert first.ce.greeks.iv == 31.4553
    assert first.ce.greeks.delta == 0.9975
    assert first.pe.trading_symbol == "NIFTY2690122000PE"

    second = snapshot.strikes[1]
    assert second.ce is not None
    assert second.pe is None  # one-sided strike handled without crashing


def test_paper_broker_get_option_chain_without_client_returns_none():
    broker = PaperBroker(market_data_client=None)
    assert broker.get_option_chain("NIFTY", date(2026, 9, 1)) is None


def test_paper_broker_get_option_chain_returns_none_on_fetch_error():
    client = FakeGrowwClient(option_chain_raise_exc=RuntimeError("network down"))
    broker = PaperBroker(market_data_client=client)
    assert broker.get_option_chain("NIFTY", date(2026, 9, 1)) is None


def test_paper_broker_get_lot_size_returns_real_value():
    client = FakeGrowwClient(instrument_response={"lot_size": "65"})
    broker = PaperBroker(market_data_client=client)
    assert broker.get_lot_size("NIFTY2690122000CE") == 65
    # get_instrument_by_exchange_and_trading_symbol() takes no `timeout` kwarg in the
    # real growwapi SDK — passing one raises TypeError on every real call (regression
    # test for that; FakeGrowwClient's **kwargs signature would silently accept it).
    assert "timeout" not in client.instrument_calls[0]


def test_live_broker_get_expiries_reauths_on_auth_exception(monkeypatch):
    failing_client = FakeGrowwClient(expiries_raise_exc=GrowwAPIAuthenticationException())
    new_client = FakeGrowwClient(expiries_response={"expiries": ["2026-09-01"]})
    monkeypatch.setattr(execution_module, "get_client", lambda: new_client)

    broker = LiveBroker(failing_client)
    result = broker.get_expiries("NIFTY")

    assert result == [date(2026, 9, 1)]
    assert broker.client is new_client


def test_live_broker_get_option_chain_reauths_on_auth_exception(monkeypatch):
    failing_client = FakeGrowwClient(option_chain_raise_exc=GrowwAPIAuthenticationException())
    new_client = FakeGrowwClient(option_chain_response=_REALISTIC_CHAIN_RESPONSE)
    monkeypatch.setattr(execution_module, "get_client", lambda: new_client)

    broker = LiveBroker(failing_client)
    snapshot = broker.get_option_chain("NIFTY", date(2026, 9, 1))

    assert snapshot is not None
    assert snapshot.strikes[0].ce.trading_symbol == "NIFTY2690122000CE"
    assert broker.client is new_client


def test_live_broker_get_lot_size_returns_real_value():
    client = FakeGrowwClient(instrument_response={"lot_size": "65"})
    broker = LiveBroker(client)
    assert broker.get_lot_size("NIFTY2690122000CE") == 65
    # get_instrument_by_exchange_and_trading_symbol() takes no `timeout` kwarg in the
    # real growwapi SDK — passing one raises TypeError on every real call (regression
    # test for that; FakeGrowwClient's **kwargs signature would silently accept it).
    assert "timeout" not in client.instrument_calls[0]


def test_live_broker_get_lot_size_returns_none_on_fetch_error():
    client = FakeGrowwClient(instrument_raise_exc=RuntimeError("network down"))
    broker = LiveBroker(client)
    assert broker.get_lot_size("NIFTY2690122000CE") is None


# --- get_recent_candles (candle-based MA/RSI redesign) --------------------

class _FixedNow(datetime):
    """Subclasses the real datetime so fromisoformat() still returns usable instances,
    but pins utcnow() to a known value — lets tests deterministically control which
    candles _drop_unclosed_trailing_candle() considers closed vs. still-forming."""
    @classmethod
    def utcnow(cls):
        return datetime(2026, 8, 28, 10, 30, 0)


_REALISTIC_CANDLES_RESPONSE = {
    "candles": [
        ["2026-08-28T10:10:00", 100.0, 101.0, 99.5, 100.5, 1000],
        ["2026-08-28T10:15:00", 100.5, 102.0, 100.0, 101.5, 1200],
        ["2026-08-28T10:20:00", 101.5, 101.8, 100.8, 101.0, 900],
    ]
}


def test_parse_candles_skips_rows_with_none_close():
    raw = {"candles": [
        ["2026-08-28T10:10:00", 100.0, 101.0, 99.5, 100.5, 1000],
        ["2026-08-28T10:15:00", None, None, None, None, None],
    ]}
    candles = _parse_candles(raw)
    assert len(candles) == 1
    assert candles[0]["close"] == 100.5


def test_paper_broker_get_recent_candles_parses_response(monkeypatch):
    monkeypatch.setattr(execution_module, "datetime", _FixedNow)
    client = FakeGrowwClient(candles_response=_REALISTIC_CANDLES_RESPONSE)
    broker = PaperBroker(market_data_client=client)

    candles = broker.get_recent_candles("RELIANCE", interval="5minute", lookback_bars=10)

    assert candles is not None
    assert [c["close"] for c in candles] == [100.5, 101.5, 101.0]
    assert candles[0]["timestamp"] == datetime(2026, 8, 28, 10, 10, 0)
    assert client.candles_calls[0]["groww_symbol"] == "NSE-RELIANCE"
    assert client.candles_calls[0]["candle_interval"] == "5minute"


def test_paper_broker_get_recent_candles_without_client_returns_none():
    broker = PaperBroker(market_data_client=None)
    assert broker.get_recent_candles("RELIANCE", interval="5minute", lookback_bars=10) is None


def test_paper_broker_get_recent_candles_returns_none_on_fetch_error():
    client = FakeGrowwClient(candles_raise_exc=RuntimeError("network down"))
    broker = PaperBroker(market_data_client=client)
    assert broker.get_recent_candles("RELIANCE", interval="5minute", lookback_bars=10) is None


def test_paper_broker_get_recent_candles_slices_to_lookback_bars(monkeypatch):
    monkeypatch.setattr(execution_module, "datetime", _FixedNow)
    client = FakeGrowwClient(candles_response=_REALISTIC_CANDLES_RESPONSE)
    broker = PaperBroker(market_data_client=client)

    candles = broker.get_recent_candles("RELIANCE", interval="5minute", lookback_bars=2)

    assert [c["close"] for c in candles] == [101.5, 101.0]  # last 2 only


def test_paper_broker_get_recent_candles_drops_still_forming_trailing_candle(monkeypatch):
    monkeypatch.setattr(execution_module, "datetime", _FixedNow)
    # Last candle at 10:28 + 5min width = 10:33, which is after the fixed "now" (10:30) —
    # still forming, must be dropped.
    response = {"candles": [
        ["2026-08-28T10:10:00", 100.0, 101.0, 99.5, 100.5, 1000],
        ["2026-08-28T10:28:00", 100.5, 102.0, 100.0, 101.5, 1200],
    ]}
    client = FakeGrowwClient(candles_response=response)
    broker = PaperBroker(market_data_client=client)

    candles = broker.get_recent_candles("RELIANCE", interval="5minute", lookback_bars=10)

    assert len(candles) == 1
    assert candles[0]["close"] == 100.5


def test_paper_broker_get_recent_candles_keeps_closed_trailing_candle(monkeypatch):
    monkeypatch.setattr(execution_module, "datetime", _FixedNow)
    # Last candle at 10:20 + 5min width = 10:25, before the fixed "now" (10:30) — closed,
    # must be kept.
    client = FakeGrowwClient(candles_response=_REALISTIC_CANDLES_RESPONSE)
    broker = PaperBroker(market_data_client=client)

    candles = broker.get_recent_candles("RELIANCE", interval="5minute", lookback_bars=10)

    assert len(candles) == 3


def test_live_broker_get_recent_candles_reauths_on_auth_exception(monkeypatch):
    monkeypatch.setattr(execution_module, "datetime", _FixedNow)
    failing_client = FakeGrowwClient(candles_raise_exc=GrowwAPIAuthenticationException())
    new_client = FakeGrowwClient(candles_response=_REALISTIC_CANDLES_RESPONSE)
    monkeypatch.setattr(execution_module, "get_client", lambda: new_client)

    broker = LiveBroker(failing_client)
    candles = broker.get_recent_candles("RELIANCE", interval="5minute", lookback_bars=10)

    assert candles is not None
    assert len(candles) == 3
    assert broker.client is new_client


def test_live_broker_get_recent_candles_returns_none_on_fetch_error():
    client = FakeGrowwClient(candles_raise_exc=RuntimeError("network down"))
    broker = LiveBroker(client)
    assert broker.get_recent_candles("RELIANCE", interval="5minute", lookback_bars=10) is None


# --- SDK call timeout (fixes a live 3-hour hang, 2026-08-31) --------------
#
# growwapi methods default to timeout=None (infinite). Every call site must pass
# GROWW_API_TIMEOUT_SEC explicitly — spot-check a representative sample of PaperBroker/
# LiveBroker methods rather than every single one, since they all follow the same pattern.

def test_paper_broker_get_ltp_passes_timeout():
    client = FakeGrowwClient()
    broker = PaperBroker(market_data_client=client)
    broker.get_ltp("RELIANCE")
    assert client.quote_calls[0]["timeout"] == execution_module.GROWW_API_TIMEOUT_SEC


def test_paper_broker_get_recent_candles_passes_timeout(monkeypatch):
    monkeypatch.setattr(execution_module, "datetime", _FixedNow)
    client = FakeGrowwClient(candles_response=_REALISTIC_CANDLES_RESPONSE)
    broker = PaperBroker(market_data_client=client)
    broker.get_recent_candles("RELIANCE", interval="5minute", lookback_bars=10)
    assert client.candles_calls[0]["timeout"] == execution_module.GROWW_API_TIMEOUT_SEC


def test_live_broker_get_ltp_passes_timeout():
    client = FakeGrowwClient()
    broker = LiveBroker(client)
    broker.get_ltp("RELIANCE")
    assert client.quote_calls[0]["timeout"] == execution_module.GROWW_API_TIMEOUT_SEC


def test_live_broker_place_order_passes_timeout():
    client = FakeGrowwClient()
    broker = LiveBroker(client)
    broker.place_order(make_order(), last_traded_price=150.0)
    assert client.calls[0]["timeout"] == execution_module.GROWW_API_TIMEOUT_SEC


def test_live_broker_get_recent_candles_passes_timeout():
    client = FakeGrowwClient(candles_response=_REALISTIC_CANDLES_RESPONSE)
    broker = LiveBroker(client)
    broker.get_recent_candles("RELIANCE", interval="5minute", lookback_bars=10)
    assert client.candles_calls[0]["timeout"] == execution_module.GROWW_API_TIMEOUT_SEC
