import pytest

from core.db import orders_repo, positions_repo
from core.models import ProposedOrder, Side, OrderType


def make_order(**overrides):
    defaults = dict(symbol="RELIANCE", side=Side.BUY, qty=1, order_type=OrderType.MARKET)
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
