import pytest

from core.db import orders_repo, positions_repo
from core.db.connection import get_connection
from core.models import OptionType, ProposedOrder, Segment, Side, OrderType


def make_order(**overrides):
    defaults = dict(symbol="RELIANCE", side=Side.BUY, qty=1, order_type=OrderType.MARKET)
    defaults.update(overrides)
    return ProposedOrder(**defaults)


def make_fno_order(**overrides):
    defaults = dict(
        symbol="NIFTY2690122000CE", side=Side.BUY, qty=1, order_type=OrderType.MARKET,
        segment=Segment.FNO, lot_size=75, option_type=OptionType.CE,
        underlying_symbol="NIFTY",
    )
    defaults.update(overrides)
    return ProposedOrder(**defaults)


def test_duplicate_idempotency_key_raises():
    order = make_order()
    orders_repo.insert_order(order, status="PROPOSED", reference_price=100.0)
    with pytest.raises(orders_repo.DuplicateIdempotencyKeyError):
        orders_repo.insert_order(order, status="PROPOSED", reference_price=100.0)


def test_only_one_open_position_per_symbol_enforced_at_db_level():
    oid1 = orders_repo.insert_order(make_order(), status="FILLED", reference_price=100.0)
    positions_repo.open_position(symbol="RELIANCE", qty=1, entry_price=100.0, entry_order_id=oid1)

    oid2 = orders_repo.insert_order(make_order(), status="FILLED", reference_price=100.0)
    with pytest.raises(Exception):
        positions_repo.open_position(symbol="RELIANCE", qty=1, entry_price=105.0, entry_order_id=oid2)


def test_deployed_capital_sums_open_positions_only():
    oid1 = orders_repo.insert_order(make_order(symbol="RELIANCE"), status="FILLED", reference_price=100.0)
    positions_repo.open_position(symbol="RELIANCE", qty=2, entry_price=100.0, entry_order_id=oid1)

    oid2 = orders_repo.insert_order(make_order(symbol="TCS"), status="FILLED", reference_price=50.0)
    pos2_id = positions_repo.open_position(symbol="TCS", qty=4, entry_price=50.0, entry_order_id=oid2)

    assert positions_repo.get_deployed_capital() == 2 * 100.0 + 4 * 50.0  # 400

    oid3 = orders_repo.insert_order(make_order(symbol="TCS", side=Side.SELL), status="FILLED", reference_price=55.0)
    positions_repo.close_position(symbol="TCS", exit_price=55.0, exit_order_id=oid3)

    assert positions_repo.get_deployed_capital() == 200.0  # only RELIANCE remains open


def test_close_position_computes_realized_pnl():
    oid1 = orders_repo.insert_order(make_order(), status="FILLED", reference_price=100.0)
    positions_repo.open_position(symbol="RELIANCE", qty=3, entry_price=100.0, entry_order_id=oid1)

    oid2 = orders_repo.insert_order(make_order(side=Side.SELL), status="FILLED", reference_price=110.0)
    pnl = positions_repo.close_position(symbol="RELIANCE", exit_price=110.0, exit_order_id=oid2)

    assert pnl == 30.0  # (110 - 100) * 3
    assert positions_repo.get_open_position("RELIANCE") is None


def test_close_position_with_no_open_position_raises():
    with pytest.raises(ValueError):
        positions_repo.close_position(symbol="GHOST", exit_price=1.0, exit_order_id=1)


# --- transaction cost model (entry_charges / exit_charges / net realized_pnl) --------

def test_open_position_stores_entry_charges():
    oid = orders_repo.insert_order(make_order(), status="FILLED", reference_price=100.0)
    positions_repo.open_position(symbol="RELIANCE", qty=1, entry_price=100.0,
                                  entry_order_id=oid, entry_charges=1.23)

    position = positions_repo.get_open_position("RELIANCE")
    assert position["entry_charges"] == 1.23


def test_open_position_defaults_entry_charges_to_zero():
    oid = orders_repo.insert_order(make_order(), status="FILLED", reference_price=100.0)
    positions_repo.open_position(symbol="RELIANCE", qty=1, entry_price=100.0, entry_order_id=oid)

    position = positions_repo.get_open_position("RELIANCE")
    assert position["entry_charges"] == 0.0


def test_close_position_deducts_entry_and_exit_charges_from_realized_pnl():
    oid1 = orders_repo.insert_order(make_order(), status="FILLED", reference_price=100.0)
    positions_repo.open_position(symbol="RELIANCE", qty=1, entry_price=100.0,
                                  entry_order_id=oid1, entry_charges=1.0)

    oid2 = orders_repo.insert_order(make_order(side=Side.SELL), status="FILLED", reference_price=110.0)
    pnl = positions_repo.close_position(symbol="RELIANCE", exit_price=110.0,
                                         exit_order_id=oid2, exit_charges=1.5)

    # gross (110 - 100) * 1 = 10.0, minus entry_charges(1.0) and exit_charges(1.5)
    assert pnl == 7.5

    closed = positions_repo.get_closed_positions()
    assert closed[0]["entry_charges"] == 1.0
    assert closed[0]["exit_charges"] == 1.5
    assert closed[0]["realized_pnl"] == 7.5


def test_win_loss_counts():
    for symbol, entry, exit_price in [("A", 100, 110), ("B", 100, 90), ("C", 100, 105)]:
        oid1 = orders_repo.insert_order(make_order(symbol=symbol), status="FILLED", reference_price=entry)
        positions_repo.open_position(symbol=symbol, qty=1, entry_price=entry, entry_order_id=oid1)
        oid2 = orders_repo.insert_order(
            make_order(symbol=symbol, side=Side.SELL), status="FILLED", reference_price=exit_price
        )
        positions_repo.close_position(symbol=symbol, exit_price=exit_price, exit_order_id=oid2)

    wins, losses = positions_repo.get_win_loss_counts()
    assert wins == 2
    assert losses == 1


# --- FNO columns (underlying_symbol, margin_used, positions.segment) ------

def test_order_underlying_symbol_round_trips():
    order = make_fno_order()
    oid = orders_repo.insert_order(order, status="PROPOSED", reference_price=25.0)

    recent = orders_repo.get_recent_orders(limit=1)
    assert recent[0]["underlying_symbol"] == "NIFTY"
    assert recent[0]["symbol"] == "NIFTY2690122000CE"


def test_update_order_status_stores_margin_used():
    order = make_fno_order()
    oid = orders_repo.insert_order(order, status="PROPOSED", reference_price=25.0)
    orders_repo.update_order_status(oid, status="FILLED", fill_price=25.0, margin_used=470.32)

    with get_connection() as conn:
        row = conn.execute("SELECT margin_used FROM orders WHERE id = ?", (oid,)).fetchone()
    assert row["margin_used"] == 470.32


def test_update_order_status_stores_charges():
    order = make_order()
    oid = orders_repo.insert_order(order, status="PROPOSED", reference_price=100.0)
    orders_repo.update_order_status(oid, status="FILLED", fill_price=100.0, charges=0.1246)

    with get_connection() as conn:
        row = conn.execute("SELECT charges FROM orders WHERE id = ?", (oid,)).fetchone()
    assert row["charges"] == 0.1246


def test_open_position_stores_segment_underlying_and_margin_used():
    order = make_fno_order()
    oid = orders_repo.insert_order(order, status="FILLED", reference_price=25.0)
    positions_repo.open_position(
        symbol=order.symbol, qty=1, entry_price=25.0, entry_order_id=oid,
        segment="FNO", underlying_symbol="NIFTY", margin_used=470.32,
    )

    open_positions = positions_repo.get_open_positions()
    assert len(open_positions) == 1
    pos = open_positions[0]
    assert pos["segment"] == "FNO"
    assert pos["underlying_symbol"] == "NIFTY"
    assert pos["margin_used"] == 470.32


def test_open_position_defaults_segment_to_cash_when_not_specified():
    oid = orders_repo.insert_order(make_order(), status="FILLED", reference_price=100.0)
    positions_repo.open_position(symbol="RELIANCE", qty=1, entry_price=100.0, entry_order_id=oid)

    open_positions = positions_repo.get_open_positions()
    assert open_positions[0]["segment"] == "CASH"
    assert open_positions[0]["underlying_symbol"] is None
    assert open_positions[0]["margin_used"] is None


def test_get_deployed_capital_uses_margin_for_fno_and_notional_for_cash():
    oid1 = orders_repo.insert_order(make_order(symbol="RELIANCE"), status="FILLED", reference_price=100.0)
    positions_repo.open_position(symbol="RELIANCE", qty=2, entry_price=100.0, entry_order_id=oid1)
    # 200 notional (CASH)

    fno_order = make_fno_order()
    oid2 = orders_repo.insert_order(fno_order, status="FILLED", reference_price=25.0)
    positions_repo.open_position(
        symbol=fno_order.symbol, qty=1, entry_price=25.0, entry_order_id=oid2,
        segment="FNO", underlying_symbol="NIFTY", margin_used=470.32,
    )
    # margin_used (470.32), NOT qty*entry_price (which would be 25.0 — the option premium,
    # not the real margin at risk) for the FNO leg

    assert positions_repo.get_deployed_capital() == pytest.approx(200.0 + 470.32)
