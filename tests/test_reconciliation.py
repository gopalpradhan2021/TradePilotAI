import logging

from config.settings import RiskConfig
from core.db import orders_repo, positions_repo
from core.execution import BrokerPositionFetchError
from core.models import ProposedOrder, Side, OrderType
from core.reconciliation import reconcile_positions
from core.risk_manager import RiskManager

test_logger = logging.getLogger("test")


def make_cfg(**overrides):
    defaults = dict(
        max_order_value_inr=100_000, max_daily_loss_inr=2_000, max_trades_per_day=10,
        max_position_qty=1_000, price_sanity_band_pct=3.0, total_capital_inr=1_000_000,
        allow_fno=False, allow_fno_index=False, fno_paper_validated=False,
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


class StubBroker:
    """Exposes only get_broker_position(symbol), matching what reconcile_positions needs."""

    def __init__(self, positions: dict[str, int] | None = None, raise_for: set[str] | None = None):
        self._positions = positions or {}
        self._raise_for = raise_for or set()
        self.calls = []

    def get_broker_position(self, symbol):
        self.calls.append(symbol)
        if symbol in self._raise_for:
            raise BrokerPositionFetchError(f"could not fetch {symbol}")
        return {"symbol": symbol, "qty": self._positions.get(symbol, 0)}


def seed_local_position(symbol: str, qty: int, entry_price: float = 100.0):
    order = ProposedOrder(symbol=symbol, side=Side.BUY, qty=qty, order_type=OrderType.MARKET)
    order_id = orders_repo.insert_order(order, status="FILLED", reference_price=entry_price)
    positions_repo.open_position(symbol=symbol, qty=qty, entry_price=entry_price, entry_order_id=order_id)


def test_reconcile_matching_positions_does_not_halt():
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(positions={"RELIANCE": 0})

    reconcile_positions(broker, risk_manager, ["RELIANCE"], logger=test_logger)

    assert risk_manager.halted is False


def test_reconcile_local_position_broker_flat_halts():
    seed_local_position("RELIANCE", qty=1)
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(positions={"RELIANCE": 0})

    reconcile_positions(broker, risk_manager, ["RELIANCE"], logger=test_logger)

    assert risk_manager.halted is True
    assert risk_manager.halt_source == "AUTO"


def test_reconcile_broker_position_local_flat_halts():
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(positions={"RELIANCE": 3})

    reconcile_positions(broker, risk_manager, ["RELIANCE"], logger=test_logger)

    assert risk_manager.halted is True


def test_reconcile_quantity_mismatch_halts():
    seed_local_position("RELIANCE", qty=5)
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(positions={"RELIANCE": 3})

    reconcile_positions(broker, risk_manager, ["RELIANCE"], logger=test_logger)

    assert risk_manager.halted is True


def test_reconcile_fetch_error_halts():
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(raise_for={"RELIANCE"})

    reconcile_positions(broker, risk_manager, ["RELIANCE"], logger=test_logger)

    assert risk_manager.halted is True
    assert risk_manager.halt_source == "AUTO"
    assert "could not fetch" in risk_manager.halt_reason


def test_reconcile_multiple_symbols_all_checked_even_after_first_mismatch():
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(positions={"RELIANCE": 3, "TCS": 0})

    reconcile_positions(broker, risk_manager, ["RELIANCE", "TCS"], logger=test_logger)

    assert set(broker.calls) == {"RELIANCE", "TCS"}


def test_reconcile_no_mismatch_across_symbols_stays_unhalted():
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(positions={"RELIANCE": 0, "TCS": 0})

    reconcile_positions(broker, risk_manager, ["RELIANCE", "TCS"], logger=test_logger)

    assert risk_manager.halted is False
