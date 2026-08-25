from datetime import date

from core.execution import PaperBroker, _build_trading_symbol
from core.models import ProposedOrder, Side, OrderType, Segment, OptionType


def make_order(**overrides):
    defaults = dict(symbol="RELIANCE", side=Side.BUY, qty=1, order_type=OrderType.MARKET)
    defaults.update(overrides)
    return ProposedOrder(**defaults)


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
