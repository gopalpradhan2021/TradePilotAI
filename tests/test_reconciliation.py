import logging

from config.settings import RiskConfig
from core.db import orders_repo, positions_repo
from core.execution import BrokerPositionFetchError
from core.models import ProposedOrder, Segment, Side, OrderType
from core.reconciliation import reconcile_orphaned_fills, reconcile_positions
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
    """Exposes only get_broker_position(symbol, segment=...), matching what
    reconcile_positions needs."""

    def __init__(self, positions: dict[str, int] | None = None, raise_for: set[str] | None = None):
        self._positions = positions or {}
        self._raise_for = raise_for or set()
        self.calls = []

    def get_broker_position(self, symbol, segment=Segment.CASH):
        self.calls.append((symbol, segment))
        if symbol in self._raise_for:
            raise BrokerPositionFetchError(f"could not fetch {symbol}")
        return {"symbol": symbol, "qty": self._positions.get(symbol, 0)}


def seed_local_position(symbol: str, qty: int, entry_price: float = 100.0,
                         segment: str = "CASH", underlying_symbol: str | None = None):
    order = ProposedOrder(symbol=symbol, side=Side.BUY, qty=qty, order_type=OrderType.MARKET)
    order_id = orders_repo.insert_order(order, status="FILLED", reference_price=entry_price)
    positions_repo.open_position(symbol=symbol, qty=qty, entry_price=entry_price, entry_order_id=order_id,
                                  segment=segment, underlying_symbol=underlying_symbol)


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

    assert {c[0] for c in broker.calls} == {"RELIANCE", "TCS"}


def test_reconcile_no_mismatch_across_symbols_stays_unhalted():
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(positions={"RELIANCE": 0, "TCS": 0})

    reconcile_positions(broker, risk_manager, ["RELIANCE", "TCS"], logger=test_logger)

    assert risk_manager.halted is False


# --- FNO reconciliation: iterates actual open position rows, not `symbols` -----

def test_reconcile_checks_open_fno_position_even_though_symbols_lists_only_cash():
    # positions.symbol for FNO is the contract, not the underlying — "NIFTY" (the
    # underlying) would never appear in `symbols` the way a CASH ticker does.
    seed_local_position("NIFTY2690122000CE", qty=1, segment="FNO", underlying_symbol="NIFTY")
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(positions={"RELIANCE": 0, "NIFTY2690122000CE": 1})

    reconcile_positions(broker, risk_manager, ["RELIANCE"], logger=test_logger)

    assert risk_manager.halted is False
    assert ("NIFTY2690122000CE", Segment.FNO) in broker.calls


def test_reconcile_fno_quantity_mismatch_halts():
    seed_local_position("NIFTY2690122000CE", qty=1, segment="FNO", underlying_symbol="NIFTY")
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(positions={"NIFTY2690122000CE": 0})  # broker says flat, local says 1

    reconcile_positions(broker, risk_manager, [], logger=test_logger)

    assert risk_manager.halted is True
    assert "NIFTY2690122000CE" in risk_manager.halt_reason


def test_reconcile_fno_fetch_error_halts():
    seed_local_position("NIFTY2690122000CE", qty=1, segment="FNO", underlying_symbol="NIFTY")
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(raise_for={"NIFTY2690122000CE"})

    reconcile_positions(broker, risk_manager, [], logger=test_logger)

    assert risk_manager.halted is True
    assert "could not fetch" in risk_manager.halt_reason


def test_reconcile_multiple_simultaneous_fno_positions_on_one_underlying_all_checked():
    # The exact case the old per-underlying-symbol design couldn't handle: two different
    # strikes on the same underlying, both open at once.
    seed_local_position("NIFTY2690122000CE", qty=1, segment="FNO", underlying_symbol="NIFTY")
    seed_local_position("NIFTY2690122200CE", qty=1, segment="FNO", underlying_symbol="NIFTY")
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(positions={"NIFTY2690122000CE": 1, "NIFTY2690122200CE": 1})

    reconcile_positions(broker, risk_manager, [], logger=test_logger)

    assert risk_manager.halted is False
    called_symbols = {c[0] for c in broker.calls}
    assert called_symbols == {"NIFTY2690122000CE", "NIFTY2690122200CE"}


def test_reconcile_fno_contract_symbol_coincidentally_in_symbols_list_stays_consistent():
    # Edge case: if an FNO contract's exact trading_symbol happened to also appear in
    # `symbols` (unusual, but not impossible), it gets checked by both the CASH loop
    # (get_open_position(symbol) finds it directly — positions_repo doesn't gate
    # lookup-by-symbol on segment) and the FNO loop over open position rows. Both checks
    # agree here, so this must NOT cause a false mismatch halt from being checked twice.
    seed_local_position("NIFTY2690122000CE", qty=1, segment="FNO", underlying_symbol="NIFTY")
    risk_manager = RiskManager(make_cfg())
    broker = StubBroker(positions={"NIFTY2690122000CE": 1})

    reconcile_positions(broker, risk_manager, ["NIFTY2690122000CE"], logger=test_logger)

    assert risk_manager.halted is False


# --- reconcile_orphaned_fills: FILLED order with no positions row ---------
# Simulates the actual reproduced bug: Orchestrator._handle_proposed_order() calling
# orders_repo.update_order_status(FILLED) and positions_repo.open_position() as two
# separate, non-atomic DB writes — a process kill between them leaves a FILLED order with
# no matching position. These tests seed exactly that state directly (skip the
# open_position() call a normal fill would have made) rather than going through the
# orchestrator, since the whole point is that the two writes are not linked.

def seed_orphaned_fill(symbol: str, qty: int = 10, fill_price: float = 100.0,
                        segment: str = "CASH", underlying_symbol: str | None = None,
                        margin_used: float | None = None, charges: float | None = None) -> int:
    order = ProposedOrder(symbol=symbol, side=Side.BUY, qty=qty, order_type=OrderType.MARKET,
                           segment=Segment[segment], underlying_symbol=underlying_symbol)
    order_id = orders_repo.insert_order(order, status="PROPOSED", reference_price=fill_price)
    orders_repo.update_order_status(order_id, status="FILLED", fill_price=fill_price,
                                     margin_used=margin_used, charges=charges)
    return order_id


def test_reconcile_orphaned_fill_reopens_position_from_order_record():
    order_id = seed_orphaned_fill("RELIANCE", qty=10, fill_price=250.0)
    risk_manager = RiskManager(make_cfg())

    reconcile_orphaned_fills(risk_manager, logger=test_logger)

    position = positions_repo.get_open_position("RELIANCE")
    assert position is not None
    assert position["qty"] == 10
    assert position["entry_price"] == 250.0
    assert position["entry_order_id"] == order_id
    assert risk_manager.halted is False


def test_reconcile_orphaned_fill_replays_trade_count_bookkeeping():
    seed_orphaned_fill("RELIANCE", qty=1, fill_price=100.0)
    risk_manager = RiskManager(make_cfg())

    reconcile_orphaned_fills(risk_manager, logger=test_logger)

    assert risk_manager._trades_today == 1


def test_reconcile_orphaned_fill_preserves_fno_segment_margin_and_charges():
    order_id = seed_orphaned_fill("NIFTY2690122000CE", qty=1, fill_price=200.0, segment="FNO",
                                   underlying_symbol="NIFTY", margin_used=15_000.0, charges=None)
    risk_manager = RiskManager(make_cfg())

    reconcile_orphaned_fills(risk_manager, logger=test_logger)

    position = positions_repo.get_open_position("NIFTY2690122000CE")
    assert position["segment"] == "FNO"
    assert position["underlying_symbol"] == "NIFTY"
    assert position["margin_used"] == 15_000.0
    assert position["entry_order_id"] == order_id


def test_reconcile_orphaned_fill_conflicting_open_position_halts_instead_of_guessing():
    # A different order already legitimately holds RELIANCE's OPEN slot — the orphaned
    # order can't be reopened without violating the one-open-position-per-symbol invariant,
    # so this must halt rather than silently drop or double-open the position.
    seed_local_position("RELIANCE", qty=5, entry_price=90.0)
    orphan_id = seed_orphaned_fill("RELIANCE", qty=10, fill_price=250.0)
    risk_manager = RiskManager(make_cfg())

    reconcile_orphaned_fills(risk_manager, logger=test_logger)

    assert risk_manager.halted is True
    assert risk_manager.halt_source == "AUTO"
    # The pre-existing legitimate position must be untouched.
    position = positions_repo.get_open_position("RELIANCE")
    assert position["qty"] == 5
    assert position["entry_order_id"] != orphan_id


def test_reconcile_orphaned_fills_noop_when_no_orphans_exist():
    seed_local_position("RELIANCE", qty=5, entry_price=90.0)
    risk_manager = RiskManager(make_cfg())

    reconcile_orphaned_fills(risk_manager, logger=test_logger)

    assert risk_manager.halted is False
    assert risk_manager._trades_today == 0


def test_reconcile_orphaned_fills_ignores_filled_order_with_no_fill_price():
    # Mirrors core/orchestrator.py's own guard: a FILLED result with fill_price is None is
    # never paired with a position by design — must not be flagged as an orphan.
    order = ProposedOrder(symbol="RELIANCE", side=Side.BUY, qty=1, order_type=OrderType.MARKET)
    orders_repo.insert_order(order, status="FILLED", reference_price=100.0)
    risk_manager = RiskManager(make_cfg())

    reconcile_orphaned_fills(risk_manager, logger=test_logger)

    assert positions_repo.get_open_position("RELIANCE") is None
    assert risk_manager.halted is False


def test_reconcile_orphaned_fills_ignores_sell_and_blocked_orders():
    sell_order = ProposedOrder(symbol="RELIANCE", side=Side.SELL, qty=1, order_type=OrderType.MARKET)
    orders_repo.insert_order(sell_order, status="FILLED", reference_price=100.0)
    blocked_order = ProposedOrder(symbol="TCS", side=Side.BUY, qty=1, order_type=OrderType.MARKET)
    orders_repo.insert_order(blocked_order, status="BLOCKED", reference_price=100.0)
    risk_manager = RiskManager(make_cfg())

    reconcile_orphaned_fills(risk_manager, logger=test_logger)

    assert risk_manager.halted is False
    assert risk_manager._trades_today == 0
