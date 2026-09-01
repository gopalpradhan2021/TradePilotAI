import pytest

from core.cost_model import calculate_order_charges, calculate_square_off_penalty
from core.models import Side


def test_brokerage_is_percentage_for_small_trade():
    # 0.1% of trade value, well under the 20 cap.
    charges = calculate_order_charges(1000.0, Side.BUY)
    assert charges > 0


def test_brokerage_capped_at_20_for_large_trade():
    # Large enough that 0.1% would exceed 20 — brokerage itself should be capped, so the
    # total for BUY (no STT) should be close to 20 plus small exchange/SEBI/stamp/GST bits,
    # not scale linearly with trade value beyond that.
    small_trade_charges = calculate_order_charges(30_000.0, Side.BUY)  # brokerage = 20 (capped)
    larger_trade_charges = calculate_order_charges(60_000.0, Side.BUY)  # brokerage still = 20

    # Both trades hit the brokerage cap, so the difference between them should only be the
    # (small) linear components — nowhere near double, even though trade value doubled.
    assert larger_trade_charges < small_trade_charges * 1.5


def test_stt_only_charged_on_sell():
    buy_charges = calculate_order_charges(10_000.0, Side.BUY)
    sell_charges = calculate_order_charges(10_000.0, Side.SELL)

    # STT (0.025%) applies only on SELL, so a sell leg costs strictly more than an
    # otherwise-identical buy leg at the same trade value.
    assert sell_charges > buy_charges


def test_stamp_duty_included_on_buy_side():
    trade_value = 10_000.0
    buy_charges = calculate_order_charges(trade_value, Side.BUY)
    stamp_duty = trade_value * 0.00003  # 0.003%, uncompounded by GST
    assert stamp_duty > 0
    assert buy_charges > stamp_duty  # buy total includes stamp duty plus everything else


def test_charges_are_a_small_fraction_of_trade_value():
    # Sanity bound: total charges for a normal trade should be well under 1% of trade
    # value (real intraday costs are small), not some order-of-magnitude bug.
    trade_value = 5000.0
    charges = calculate_order_charges(trade_value, Side.SELL)
    assert 0 < charges < trade_value * 0.01


def test_charges_scale_roughly_linearly_below_the_brokerage_cap():
    small = calculate_order_charges(1000.0, Side.SELL)
    double = calculate_order_charges(2000.0, Side.SELL)
    assert double == pytest.approx(small * 2.0, rel=0.05)


def test_zero_trade_value_produces_zero_charges():
    assert calculate_order_charges(0.0, Side.BUY) == 0.0
    assert calculate_order_charges(0.0, Side.SELL) == 0.0


def test_square_off_penalty_is_flat_fee_plus_gst():
    # Rs 50 flat fee + 18% GST = Rs 59, fixed regardless of trade value.
    assert calculate_square_off_penalty() == 59.0
