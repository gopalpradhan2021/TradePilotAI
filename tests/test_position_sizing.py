from core.position_sizing import calculate_entry_qty


def test_zero_or_negative_entry_price_returns_zero():
    assert calculate_entry_qty(0.0, 10_000.0, 20_000.0, 50) == 0
    assert calculate_entry_qty(-5.0, 10_000.0, 20_000.0, 50) == 0


def test_value_cap_binds():
    # 100/share, value cap only allows 5 shares (500), capital/qty caps are looser.
    qty = calculate_entry_qty(entry_price=100.0, available_capital=100_000.0,
                               max_order_value_inr=500.0, max_position_qty=1_000)
    assert qty == 5


def test_available_capital_binds():
    # 100/share, only 350 available — floors to 3 shares even though value cap allows more.
    qty = calculate_entry_qty(entry_price=100.0, available_capital=350.0,
                               max_order_value_inr=100_000.0, max_position_qty=1_000)
    assert qty == 3


def test_max_position_qty_binds():
    # Both money caps would allow far more than 7 shares, but the share-count cap wins.
    qty = calculate_entry_qty(entry_price=10.0, available_capital=100_000.0,
                               max_order_value_inr=100_000.0, max_position_qty=7)
    assert qty == 7


def test_available_capital_at_or_below_zero_returns_zero_not_negative():
    assert calculate_entry_qty(100.0, 0.0, 100_000.0, 1_000) == 0
    assert calculate_entry_qty(100.0, -500.0, 100_000.0, 1_000) == 0


def test_fractional_price_floors_not_rounds():
    # 333.33/share, 1000 available -> 3 shares fit (999.99), not 3.0003 rounded up to 4.
    qty = calculate_entry_qty(entry_price=333.33, available_capital=1000.0,
                               max_order_value_inr=100_000.0, max_position_qty=1_000)
    assert qty == 3


def test_all_caps_equal_returns_exactly_that_boundary_qty():
    # value cap = capital = 1000 at price 100 -> exactly 10 shares, not 9.
    qty = calculate_entry_qty(entry_price=100.0, available_capital=1000.0,
                               max_order_value_inr=1000.0, max_position_qty=10)
    assert qty == 10
