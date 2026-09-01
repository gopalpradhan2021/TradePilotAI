from datetime import date, datetime

from core.models import OptionChainSnapshot, OptionGreeks, OptionQuote, OptionType, Segment, Side, StrikeQuote
from strategies.iv_oi_strategy import IvOiStrategy, IvOiParams


def make_quote(trading_symbol, delta, iv, oi, ltp=100.0, gamma=0.0, theta=0.0, vega=0.0, rho=0.0):
    return OptionQuote(
        trading_symbol=trading_symbol, ltp=ltp, open_interest=oi, volume=0,
        greeks=OptionGreeks(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho, iv=iv),
    )


def make_chain(underlying_ltp=24000.0, expiry=date(2026, 9, 1), strikes=None):
    return OptionChainSnapshot(
        underlying="NIFTY", underlying_ltp=underlying_ltp, expiry_date=expiry,
        fetched_at=datetime(2026, 8, 28, 10, 0), strikes=strikes or [],
    )


def make_params(**overrides):
    defaults = dict(
        iv_history_len=3, min_oi_buildup_pct=5.0, entry_delta_min=0.35, entry_delta_max=0.65,
        exit_delta_floor=0.15, iv_collapse_exit_pct=25.0, stop_loss_pct=30.0,
    )
    defaults.update(overrides)
    return IvOiParams(**defaults)


def fixed_lot_size_fn(lot_size=75):
    return lambda trading_symbol: lot_size


def test_no_signal_while_warming_up_iv_history():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn())
    chain = make_chain(strikes=[StrikeQuote(
        strike=24000.0, ce=make_quote("NIFTY26SEP24000CE", delta=0.5, iv=20.0, oi=1000), pe=None,
    )])

    order = strategy.decide_fno("NIFTY", chain)

    assert order is None
    debug = strategy.get_debug_info("NIFTY")
    assert debug["warmed_up"] is False
    assert debug["iv_history_collected"] == 1
    assert debug["current_atm_iv"] == 20.0
    assert debug["atm_iv_ceiling"] == 20.0


def test_get_debug_info_before_any_chain_shows_no_iv_context():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn())
    debug = strategy.get_debug_info("NIFTY")
    assert debug["current_atm_iv"] is None
    assert debug["atm_iv_ceiling"] is None


def _warm_up(strategy, chain_builder, cycles=3):
    """Feeds `cycles` snapshots with strictly increasing OI-free warmup values so the IV
    history fills without accidentally satisfying the OI-buildup entry condition too."""
    last = None
    for i in range(cycles):
        chain = chain_builder(iv=20.0 + i)  # distinct IVs so max()/current comparisons are meaningful
        last = strategy.decide_fno("NIFTY", chain)
    return last


def test_buy_signal_on_oi_buildup_with_healthy_delta_and_non_peak_iv():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn(lot_size=75))

    def chain_builder(iv, oi=1000):
        return make_chain(strikes=[StrikeQuote(
            strike=24000.0,
            ce=make_quote("NIFTY26SEP24000CE", delta=0.5, iv=iv, oi=oi, ltp=150.0),
            pe=make_quote("NIFTY26SEP24000PE", delta=-0.5, iv=iv, oi=oi, ltp=140.0),
        )])

    # Warm up with a peak IV of 22.0 on the last warmup cycle, oi flat at 1000 (no buildup yet)
    strategy.decide_fno("NIFTY", chain_builder(iv=20.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=21.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=22.0))

    # Now: IV drops below the recent high (22.0), and OI jumps 1000 -> 1100 (+10%, above the 5% floor)
    order = strategy.decide_fno("NIFTY", chain_builder(iv=18.0, oi=1100))

    assert order is not None
    assert order.side == Side.BUY
    assert order.symbol == "NIFTY26SEP24000CE"  # CE checked before PE, both qualify here
    assert order.underlying_symbol == "NIFTY"
    assert order.segment == Segment.FNO
    assert order.option_type == OptionType.CE
    assert order.lot_size == 75
    assert order.strike_price == 24000.0
    assert "OI buildup" in order.reason


def test_no_buy_when_oi_buildup_below_threshold():
    strategy = IvOiStrategy(params=make_params(min_oi_buildup_pct=20.0), lot_size_fn=fixed_lot_size_fn())

    def chain_builder(iv, oi=1000):
        return make_chain(strikes=[StrikeQuote(
            strike=24000.0, ce=make_quote("NIFTY26SEP24000CE", delta=0.5, iv=iv, oi=oi), pe=None,
        )])

    strategy.decide_fno("NIFTY", chain_builder(iv=20.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=21.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=22.0))

    # Only +5% OI buildup, below the 20% threshold configured for this test
    order = strategy.decide_fno("NIFTY", chain_builder(iv=18.0, oi=1050))

    assert order is None


def test_no_buy_when_delta_outside_entry_band():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn())

    def chain_builder(iv, oi=1000, delta=0.9):  # deep ITM, outside 0.35-0.65 band
        return make_chain(strikes=[StrikeQuote(
            strike=24000.0, ce=make_quote("NIFTY26SEP24000CE", delta=delta, iv=iv, oi=oi), pe=None,
        )])

    strategy.decide_fno("NIFTY", chain_builder(iv=20.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=21.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=22.0))

    order = strategy.decide_fno("NIFTY", chain_builder(iv=18.0, oi=1200))

    assert order is None


def test_no_buy_when_iv_at_recent_high():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn())

    def chain_builder(iv, oi=1000):
        return make_chain(strikes=[StrikeQuote(
            strike=24000.0, ce=make_quote("NIFTY26SEP24000CE", delta=0.5, iv=iv, oi=oi), pe=None,
        )])

    strategy.decide_fno("NIFTY", chain_builder(iv=20.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=21.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=22.0))

    # New high IV (23.0, above the prior ceiling of 22.0) — should be rejected even with buildup
    order = strategy.decide_fno("NIFTY", chain_builder(iv=23.0, oi=1200))

    assert order is None


def test_no_buy_when_lot_size_fn_returns_none():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=lambda symbol: None)

    def chain_builder(iv, oi=1000):
        return make_chain(strikes=[StrikeQuote(
            strike=24000.0, ce=make_quote("NIFTY26SEP24000CE", delta=0.5, iv=iv, oi=oi), pe=None,
        )])

    strategy.decide_fno("NIFTY", chain_builder(iv=20.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=21.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=22.0))
    order = strategy.decide_fno("NIFTY", chain_builder(iv=18.0, oi=1200))

    assert order is None


def test_no_buy_when_lot_size_fn_not_configured():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=None)

    def chain_builder(iv, oi=1000):
        return make_chain(strikes=[StrikeQuote(
            strike=24000.0, ce=make_quote("NIFTY26SEP24000CE", delta=0.5, iv=iv, oi=oi), pe=None,
        )])

    strategy.decide_fno("NIFTY", chain_builder(iv=20.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=21.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=22.0))
    order = strategy.decide_fno("NIFTY", chain_builder(iv=18.0, oi=1200))

    assert order is None


def _enter_position(strategy):
    def chain_builder(iv, oi=1000):
        return make_chain(strikes=[StrikeQuote(
            strike=24000.0,
            ce=make_quote("NIFTY26SEP24000CE", delta=0.5, iv=iv, oi=oi, ltp=150.0),
            pe=None,
        )])
    strategy.decide_fno("NIFTY", chain_builder(iv=20.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=21.0))
    strategy.decide_fno("NIFTY", chain_builder(iv=22.0))
    order = strategy.decide_fno("NIFTY", chain_builder(iv=18.0, oi=1200))
    assert order is not None and order.side == Side.BUY
    return order


def test_sell_signal_on_delta_decay():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn(lot_size=75))
    _enter_position(strategy)

    # Held contract's delta has decayed below the 0.15 floor
    chain = make_chain(strikes=[StrikeQuote(
        strike=24000.0, ce=make_quote("NIFTY26SEP24000CE", delta=0.10, iv=18.0, oi=1200, ltp=140.0), pe=None,
    )])
    order = strategy.decide_fno("NIFTY", chain)

    assert order is not None
    assert order.side == Side.SELL
    assert order.symbol == "NIFTY26SEP24000CE"
    assert "Delta decayed" in order.reason


def test_sell_signal_on_iv_collapse():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn(lot_size=75))
    _enter_position(strategy)  # entry_iv = 18.0

    # IV collapsed to 12.0, well below 18.0 * 0.75 = 13.5
    chain = make_chain(strikes=[StrikeQuote(
        strike=24000.0, ce=make_quote("NIFTY26SEP24000CE", delta=0.5, iv=12.0, oi=1200, ltp=140.0), pe=None,
    )])
    order = strategy.decide_fno("NIFTY", chain)

    assert order is not None
    assert order.side == Side.SELL
    assert "IV collapsed" in order.reason


def test_sell_signal_on_stop_loss():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn(lot_size=75))
    _enter_position(strategy)  # entry_price = 150.0

    # Premium fell to 100.0, below 150.0 * 0.70 = 105.0
    chain = make_chain(strikes=[StrikeQuote(
        strike=24000.0, ce=make_quote("NIFTY26SEP24000CE", delta=0.5, iv=18.0, oi=1200, ltp=100.0), pe=None,
    )])
    order = strategy.decide_fno("NIFTY", chain)

    assert order is not None
    assert order.side == Side.SELL
    assert "Stop-loss" in order.reason


def test_no_exit_when_held_contract_missing_from_chain():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn(lot_size=75))
    _enter_position(strategy)

    # A chain that no longer contains the held contract at all (e.g. expiry rolled)
    chain = make_chain(strikes=[StrikeQuote(
        strike=24000.0, ce=make_quote("NIFTY26OCT24000CE", delta=0.5, iv=18.0, oi=1200, ltp=140.0), pe=None,
    )])
    order = strategy.decide_fno("NIFTY", chain)

    assert order is None


def test_no_exit_when_no_exit_condition_met():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn(lot_size=75))
    _enter_position(strategy)

    # Healthy delta, IV barely moved, premium unchanged — nothing should trigger
    chain = make_chain(strikes=[StrikeQuote(
        strike=24000.0, ce=make_quote("NIFTY26SEP24000CE", delta=0.5, iv=17.5, oi=1200, ltp=150.0), pe=None,
    )])
    order = strategy.decide_fno("NIFTY", chain)

    assert order is None


def test_restore_position_marks_in_position_without_managing_exits():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn())
    strategy.restore_position("NIFTY", entry_price=150.0)

    debug = strategy.get_debug_info("NIFTY")
    assert debug["in_position"] is True
    assert debug["entry_price"] == 150.0
    assert debug["held_trading_symbol"] is None  # not recoverable from restore alone

    # decide_fno should not propose a fresh entry while (falsely) marked in_position
    chain = make_chain(strikes=[StrikeQuote(
        strike=24000.0, ce=make_quote("NIFTY26SEP24000CE", delta=0.5, iv=20.0, oi=1000, ltp=150.0), pe=None,
    )])
    order = strategy.decide_fno("NIFTY", chain)
    assert order is None  # _maybe_exit runs, finds no matching held symbol, returns None


def test_no_signal_when_no_strikes_in_chain():
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn())
    chain = make_chain(strikes=[])
    assert strategy.decide_fno("NIFTY", chain) is None


def test_empty_strikes_logs_instead_of_failing_silently(caplog):
    # Found live 2026-09-01: an underlying's own expiry day resolves "nearest upcoming" to
    # today, but Groww's live chain for an already-expired contract comes back empty
    # post-close — this used to look identical to a genuinely broken fetch in the logs.
    strategy = IvOiStrategy(params=make_params(), lot_size_fn=fixed_lot_size_fn())
    chain = make_chain(strikes=[])
    with caplog.at_level("INFO"):
        strategy.decide_fno("NIFTY", chain)
    assert any("no strikes in chain" in r.message for r in caplog.records)
