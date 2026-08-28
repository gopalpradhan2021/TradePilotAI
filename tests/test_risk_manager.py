from datetime import date, timedelta

import core.risk_manager as risk_manager_module
from config.settings import RiskConfig
from core.db import orders_repo, positions_repo
from core.models import MarginQuote, ProposedOrder, Side, OrderType, Segment
from core.risk_manager import RiskManager


class FakeMarginProvider:
    """GrowwMarginProvider.get_order_margin() never raises — it catches everything
    internally and returns None on any fetch failure (see core/margin_provider.py).
    This fake matches that same None-on-failure contract rather than raising."""
    def __init__(self, quote: MarginQuote | None = None):
        self._quote = quote
        self.calls = []

    def get_order_margin(self, order):
        self.calls.append(order)
        return self._quote


def make_cfg(**overrides):
    defaults = dict(
        max_order_value_inr=100_000,
        max_daily_loss_inr=2_000,
        max_trades_per_day=10,
        max_position_qty=1_000,
        price_sanity_band_pct=3.0,
        total_capital_inr=1_000_000,
        allow_fno=False,
        allow_fno_index=False,
        fno_paper_validated=False,
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


def make_order(**overrides):
    defaults = dict(symbol="RELIANCE", side=Side.BUY, qty=1, order_type=OrderType.MARKET)
    defaults.update(overrides)
    return ProposedOrder(**defaults)


def check(risk_manager, order, ltp):
    """Mirrors what the orchestrator does: insert the order row first, then check it."""
    order_id = orders_repo.insert_order(order, status="PROPOSED", reference_price=ltp)
    return risk_manager.check(order, last_traded_price=ltp, order_id=order_id)


def test_valid_order_is_approved():
    rm = RiskManager(make_cfg())
    result = check(rm, make_order(), ltp=100.0)
    assert result.approved
    assert result.reasons == []


def test_fno_rejected_when_not_allowed():
    rm = RiskManager(make_cfg(allow_fno=False))
    order = make_order(segment=Segment.FNO, lot_size=50)
    result = check(rm, order, ltp=100.0)
    assert not result.approved
    assert any("F&O" in r for r in result.reasons)


def test_fno_allowed_when_enabled():
    # PAPER mode, no margin_provider: falls back to the naive notional-value math (same
    # as CASH) so this test stays focused on the allow_fno gate itself, not margin sizing
    # — see test_risk_manager.py's FNO-margin-specific tests for that.
    rm = RiskManager(make_cfg(allow_fno=True, max_position_qty=100), mode="PAPER")
    order = make_order(segment=Segment.FNO, lot_size=50, qty=1)
    result = check(rm, order, ltp=100.0)
    assert result.approved


def test_non_positive_qty_rejected():
    rm = RiskManager(make_cfg())
    result = check(rm, make_order(qty=0), ltp=100.0)
    assert not result.approved
    assert any("Quantity" in r for r in result.reasons)


def test_max_position_qty_uses_total_units_not_qty():
    # FNO: total_units = qty * lot_size, so qty=2 with lot_size=50 -> 100 units
    rm = RiskManager(make_cfg(allow_fno=True, max_position_qty=50))
    order = make_order(segment=Segment.FNO, qty=2, lot_size=50)
    result = check(rm, order, ltp=10.0)
    assert not result.approved
    assert any("exceeds max_position_qty" in r for r in result.reasons)


# --- FNO margin-aware sizing (Phase A) -------------------------------------

def test_fno_live_rejected_when_no_margin_provider_configured():
    rm = RiskManager(make_cfg(allow_fno=True, max_position_qty=100), mode="LIVE")
    order = make_order(segment=Segment.FNO, lot_size=50, qty=1)
    result = check(rm, order, ltp=100.0)
    assert not result.approved
    assert any("margin check unavailable" in r for r in result.reasons)


def test_fno_paper_falls_back_to_notional_when_no_margin_provider_configured():
    rm = RiskManager(make_cfg(allow_fno=True, max_position_qty=100), mode="PAPER")
    order = make_order(segment=Segment.FNO, lot_size=50, qty=1)
    result = check(rm, order, ltp=100.0)
    assert result.approved
    assert result.margin_quote is None


def test_fno_approved_when_margin_within_caps():
    quote = MarginQuote(required_margin=5_000, available_margin=50_000)
    rm = RiskManager(
        make_cfg(allow_fno=True, max_position_qty=100, max_order_value_inr=10_000),
        margin_provider=FakeMarginProvider(quote=quote),
    )
    order = make_order(segment=Segment.FNO, lot_size=50, qty=1)
    result = check(rm, order, ltp=100.0)
    assert result.approved
    assert result.margin_quote == quote


def test_fno_rejected_when_required_margin_exceeds_cap():
    quote = MarginQuote(required_margin=20_000, available_margin=50_000)
    rm = RiskManager(
        make_cfg(allow_fno=True, max_position_qty=100, max_order_value_inr=10_000),
        margin_provider=FakeMarginProvider(quote=quote),
    )
    order = make_order(segment=Segment.FNO, lot_size=50, qty=1)
    result = check(rm, order, ltp=100.0)
    assert not result.approved
    assert any("Required margin" in r and "exceeds cap" in r for r in result.reasons)


def test_fno_rejected_when_required_margin_exceeds_available_broker_margin():
    quote = MarginQuote(required_margin=5_000, available_margin=1_000)
    rm = RiskManager(
        make_cfg(allow_fno=True, max_position_qty=100, max_order_value_inr=10_000),
        margin_provider=FakeMarginProvider(quote=quote),
    )
    order = make_order(segment=Segment.FNO, lot_size=50, qty=1)
    result = check(rm, order, ltp=100.0)
    assert not result.approved
    assert any("exceeds available broker margin" in r for r in result.reasons)


def test_fno_rejected_when_margin_fetch_fails_does_not_halt():
    rm = RiskManager(
        make_cfg(allow_fno=True, max_position_qty=100),
        margin_provider=FakeMarginProvider(quote=None),
    )
    order = make_order(segment=Segment.FNO, lot_size=50, qty=1)
    result = check(rm, order, ltp=100.0)
    assert not result.approved
    assert any("Could not fetch margin details" in r for r in result.reasons)
    assert not rm.halted  # fails closed on this order only — never halts the bot


def test_daily_trade_count_cap():
    rm = RiskManager(make_cfg(max_trades_per_day=1))
    rm._trades_today = 1  # simulate a trade already recorded today
    result = check(rm, make_order(), ltp=100.0)
    assert not result.approved
    assert any("Daily trade count limit" in r for r in result.reasons)


def test_daily_trade_count_cap_not_enforced_in_paper_mode():
    """Paper trading risks no real money — the cap exists to protect capital, so it's
    deliberately not enforced there, letting the strategy's true trade frequency show
    through for future tuning."""
    rm = RiskManager(make_cfg(max_trades_per_day=1), mode="PAPER")
    rm._trades_today = 5  # already well past the configured cap
    result = check(rm, make_order(), ltp=100.0)
    assert result.approved


def test_daily_trade_count_cap_still_enforced_when_mode_unspecified():
    """mode defaults to LIVE (the conservative choice) so any caller that doesn't pass it
    — including any code added later — keeps the cap enforced rather than silently losing it."""
    rm = RiskManager(make_cfg(max_trades_per_day=1))
    rm._trades_today = 1
    result = check(rm, make_order(), ltp=100.0)
    assert not result.approved


def test_order_value_cap():
    rm = RiskManager(make_cfg(max_order_value_inr=500))
    result = check(rm, make_order(qty=10), ltp=100.0)  # order value = 1000
    assert not result.approved
    assert any("exceeds cap" in r for r in result.reasons)


def test_total_capital_cap_accounts_for_deployed_capital():
    rm = RiskManager(make_cfg(total_capital_inr=1_000, max_order_value_inr=10_000))
    # Simulate an already-open position worth 600 deployed capital.
    order_id = orders_repo.insert_order(make_order(), status="FILLED", reference_price=60.0)
    positions_repo.open_position(symbol="INFY", qty=10, entry_price=60.0, entry_order_id=order_id)

    # New BUY of 500 would push deployed capital to 1100 > cap of 1000.
    result = check(rm, make_order(symbol="TCS", qty=5), ltp=100.0)
    assert not result.approved
    assert any("exceeding" in r and "total capital cap" in r for r in result.reasons)


def test_sell_orders_not_subject_to_capital_cap():
    rm = RiskManager(make_cfg(total_capital_inr=10, max_order_value_inr=100_000))
    result = check(rm, make_order(side=Side.SELL, qty=1), ltp=100.0)
    assert result.approved


def test_no_reference_price_rejected():
    rm = RiskManager(make_cfg())
    result = check(rm, make_order(), ltp=None)
    assert not result.approved
    assert any("No reference price" in r for r in result.reasons)


def test_limit_price_outside_sanity_band_rejected():
    rm = RiskManager(make_cfg(price_sanity_band_pct=3.0))
    order = make_order(order_type=OrderType.LIMIT, limit_price=200.0)
    result = check(rm, order, ltp=100.0)  # 200 is way outside 97-103 band
    assert not result.approved
    assert any("sanity band" in r for r in result.reasons)


def test_limit_price_inside_sanity_band_approved():
    rm = RiskManager(make_cfg(price_sanity_band_pct=3.0))
    order = make_order(order_type=OrderType.LIMIT, limit_price=101.0)
    result = check(rm, order, ltp=100.0)
    assert result.approved


def test_halted_rejects_everything_regardless_of_other_checks():
    rm = RiskManager(make_cfg())
    rm.manual_halt("test halt")
    result = check(rm, make_order(), ltp=100.0)
    assert not result.approved
    assert "halted" in result.reasons[0].lower()


def test_record_fill_halts_on_daily_loss_breach():
    rm = RiskManager(make_cfg(max_daily_loss_inr=1_000))
    rm.record_fill(side=Side.SELL, order_value=500.0, pnl_delta=-1_200.0)
    assert rm.halted is True
    assert "Daily loss limit breached" in rm.halt_reason

    # Halt must reject subsequent orders too.
    result = check(rm, make_order(), ltp=100.0)
    assert not result.approved


def test_halt_persists_across_new_risk_manager_instance():
    rm1 = RiskManager(make_cfg())
    rm1.manual_halt("persisted halt")

    rm2 = RiskManager(make_cfg())  # simulates a process restart
    assert rm2.halted is True
    assert rm2.halt_reason == "persisted halt"


def test_day_rollover_resets_counters_and_clears_halt():
    rm = RiskManager(make_cfg())
    # Simulate the process having run yesterday and gotten halted then.
    rm._current_day = date.today() - timedelta(days=1)
    rm._sync_daily_state()
    rm.manual_halt("yesterday's halt")  # persists against yesterday's daily_summary row
    assert rm.halted is True

    # Now the wall clock has moved to a new day.
    rm._roll_day_if_needed()

    assert rm.halted is False
    assert rm._trades_today == 0
    assert rm._realized_pnl_today == 0


def test_today_fn_injection_drives_day_rollover():
    """scripts/backtest.py relies on this: an injected clock, not the real wall
    clock, must control when daily counters reset."""
    sim_day = date(2026, 1, 1)
    rm = RiskManager(make_cfg(), today_fn=lambda: sim_day)
    rm.manual_halt("day one halt")
    assert rm.halted is True

    sim_day = date(2026, 1, 2)
    rm._roll_day_if_needed()

    assert rm.halted is False
    assert rm._trades_today == 0


def test_refresh_halt_state_rolls_day_first():
    """A halt from a prior (simulated) day must not read as still-halted on a new
    day just because refresh_halt_state() ran before any order was checked —
    check() is the only other place that used to roll the day, so a day with
    zero proposed orders would otherwise never advance."""
    sim_day = date(2026, 1, 1)
    rm = RiskManager(make_cfg(), today_fn=lambda: sim_day)
    rm.manual_halt("day one halt")
    assert rm.halted is True

    sim_day = date(2026, 1, 2)
    rm.refresh_halt_state()

    assert rm.halted is False


def test_record_fill_increments_trade_count_and_pnl():
    rm = RiskManager(make_cfg(max_daily_loss_inr=10_000))
    rm.record_fill(side=Side.BUY, order_value=100.0, pnl_delta=0.0)
    rm.record_fill(side=Side.SELL, order_value=100.0, pnl_delta=50.0)
    assert rm._trades_today == 2
    assert rm._realized_pnl_today == 50.0


def test_daily_loss_breach_triggers_exactly_one_notification(monkeypatch):
    calls = []
    monkeypatch.setattr(
        risk_manager_module, "send_notification_raw",
        lambda topic, msg: calls.append(msg) or True,
    )
    rm = RiskManager(make_cfg(max_daily_loss_inr=1_000), ntfy_topic="my-topic")

    rm.record_fill(side=Side.SELL, order_value=500.0, pnl_delta=-1_200.0)

    assert len(calls) == 1
    assert "TRADING HALTED" in calls[0]


def test_manual_halt_triggers_exactly_one_notification(monkeypatch):
    calls = []
    monkeypatch.setattr(
        risk_manager_module, "send_notification_raw",
        lambda topic, msg: calls.append(msg) or True,
    )
    rm = RiskManager(make_cfg(), ntfy_topic="my-topic")

    rm.manual_halt("test halt reason")

    assert len(calls) == 1
    assert "test halt reason" in calls[0]


def test_manual_halt_sets_source_manual():
    rm = RiskManager(make_cfg())
    rm.manual_halt("test")
    assert rm.halt_source == "MANUAL"


def test_record_fill_daily_loss_halt_sets_source_auto():
    rm = RiskManager(make_cfg(max_daily_loss_inr=1_000))
    rm.record_fill(side=Side.SELL, order_value=500.0, pnl_delta=-1_200.0)
    assert rm.halt_source == "AUTO"


def test_resume_clears_manual_halt():
    rm = RiskManager(make_cfg())
    rm.manual_halt("test")
    rm.resume("all clear")

    assert rm.halted is False
    result = check(rm, make_order(), ltp=100.0)
    assert result.approved


def test_resume_raises_on_automatic_halt():
    rm = RiskManager(make_cfg(max_daily_loss_inr=1_000))
    rm.record_fill(side=Side.SELL, order_value=500.0, pnl_delta=-1_200.0)

    try:
        rm.resume("trying to bypass")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass

    assert rm.halted is True


def test_check_picks_up_external_halt_via_db():
    rm1 = RiskManager(make_cfg())
    rm2 = RiskManager(make_cfg())  # simulates scripts/halt_bot.py running separately

    rm2.manual_halt("external halt")

    result = check(rm1, make_order(), ltp=100.0)
    assert not result.approved
    assert rm1.halt_source == "MANUAL"


def test_check_picks_up_external_resume_via_db():
    rm1 = RiskManager(make_cfg())
    rm1.manual_halt("initial halt")

    rm2 = RiskManager(make_cfg())  # simulates scripts/resume_bot.py running separately
    rm2.resume("external resume")

    result = check(rm1, make_order(), ltp=100.0)
    assert result.approved


def test_halt_circuit_breaker_sets_source_auto_and_notifies(monkeypatch):
    calls = []
    monkeypatch.setattr(
        risk_manager_module, "send_notification_raw",
        lambda topic, msg: calls.append(msg) or True,
    )
    rm = RiskManager(make_cfg(), ntfy_topic="my-topic")

    rm.halt_circuit_breaker(5)

    assert rm.halted is True
    assert rm.halt_source == "AUTO"
    assert len(calls) == 1
    assert "Circuit breaker" in calls[0]
    assert "5" in calls[0]


def test_halt_circuit_breaker_cannot_be_manually_resumed():
    rm = RiskManager(make_cfg())
    rm.halt_circuit_breaker(5)

    try:
        rm.resume("trying to bypass")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass

    assert rm.halted is True
