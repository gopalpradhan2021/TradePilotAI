import strategies.ma_rsi_strategy as mod
from core.models import Side
from strategies.ma_rsi_strategy import MARsiParams, MARsiStrategy, _sma, _rsi


# --- pure indicator math -----------------------------------------------

def test_sma_returns_none_when_not_enough_data():
    assert _sma([1, 2], 3) is None


def test_sma_averages_the_last_n_prices():
    assert _sma([1, 2, 3, 4, 5], 3) == (3 + 4 + 5) / 3


def test_rsi_returns_none_when_not_enough_data():
    assert _rsi([1, 2, 3], 5) is None


def test_rsi_is_100_when_all_gains():
    prices = [float(i) for i in range(1, 16)]  # strictly increasing, window=14
    assert _rsi(prices, 14) == 100.0


def test_rsi_is_0_when_all_losses():
    prices = [float(i) for i in range(15, 0, -1)]  # strictly decreasing
    assert _rsi(prices, 14) == 0.0


# --- state-machine logic, with indicators mocked for determinism -------

def _patch_indicators(monkeypatch, short_ma, long_ma, rsi):
    monkeypatch.setattr(
        mod, "_sma",
        lambda prices, window: short_ma if window == mod.SHORT_WINDOW else long_ma,
    )
    monkeypatch.setattr(mod, "_rsi", lambda prices, window: rsi)


def test_no_signal_while_warming_up(monkeypatch):
    _patch_indicators(monkeypatch, short_ma=None, long_ma=None, rsi=None)
    strategy = MARsiStrategy()
    assert strategy.decide("RELIANCE", 100.0) is None


def test_buy_signal_on_crossover_up_with_healthy_rsi(monkeypatch):
    strategy = MARsiStrategy()

    _patch_indicators(monkeypatch, short_ma=90, long_ma=100, rsi=50)
    assert strategy.decide("RELIANCE", 100.0) is None  # establishes prev MAs, no crossover yet

    _patch_indicators(monkeypatch, short_ma=110, long_ma=100, rsi=55)
    order = strategy.decide("RELIANCE", 102.0)

    assert order is not None
    assert order.side == Side.BUY
    assert strategy._get_state("RELIANCE").in_position is True
    assert strategy._get_state("RELIANCE").entry_price == 102.0


def test_no_buy_when_crossover_up_but_rsi_outside_entry_range(monkeypatch):
    strategy = MARsiStrategy()

    _patch_indicators(monkeypatch, short_ma=90, long_ma=100, rsi=50)
    strategy.decide("RELIANCE", 100.0)

    _patch_indicators(monkeypatch, short_ma=110, long_ma=100, rsi=85)  # overbought at entry
    order = strategy.decide("RELIANCE", 102.0)

    assert order is None
    assert strategy._get_state("RELIANCE").in_position is False


def _enter_position(strategy, monkeypatch, entry_price=100.0):
    _patch_indicators(monkeypatch, short_ma=90, long_ma=100, rsi=50)
    strategy.decide("RELIANCE", entry_price)
    _patch_indicators(monkeypatch, short_ma=110, long_ma=100, rsi=55)
    order = strategy.decide("RELIANCE", entry_price)
    assert order is not None and order.side == Side.BUY
    return order


def test_sell_signal_on_crossover_down(monkeypatch):
    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)

    _patch_indicators(monkeypatch, short_ma=95, long_ma=100, rsi=60)
    order = strategy.decide("RELIANCE", 99.0)

    assert order is not None
    assert order.side == Side.SELL
    assert "crossover DOWN" in order.reason
    assert strategy._get_state("RELIANCE").in_position is False


def test_sell_signal_on_rsi_overbought_without_crossover_down(monkeypatch):
    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)

    # short_ma still above long_ma (no crossover down), but RSI overbought
    _patch_indicators(monkeypatch, short_ma=120, long_ma=100, rsi=80)
    order = strategy.decide("RELIANCE", 105.0)

    assert order is not None
    assert order.side == Side.SELL
    assert "RSI overbought" in order.reason


def test_sell_signal_on_stop_loss(monkeypatch):
    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)

    # no crossover down, RSI not overbought, but price dropped 2%+ below entry
    _patch_indicators(monkeypatch, short_ma=120, long_ma=100, rsi=50)
    order = strategy.decide("RELIANCE", 97.5)  # <= 100 * 0.98

    assert order is not None
    assert order.side == Side.SELL
    assert "Stop-loss hit" in order.reason


def test_no_sell_when_in_position_and_no_exit_condition_met(monkeypatch):
    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)

    _patch_indicators(monkeypatch, short_ma=120, long_ma=100, rsi=50)
    order = strategy.decide("RELIANCE", 99.0)  # above stop-loss threshold

    assert order is None
    assert strategy._get_state("RELIANCE").in_position is True


def test_none_price_is_ignored():
    strategy = MARsiStrategy()
    assert strategy.decide("RELIANCE", None) is None
    assert "RELIANCE" not in strategy._state


# --- crossover gap threshold (whipsaw fix) ------------------------------

def test_no_buy_when_crossover_gap_below_min_threshold(monkeypatch):
    strategy = MARsiStrategy()

    _patch_indicators(monkeypatch, short_ma=100.0, long_ma=100.0, rsi=50)
    strategy.decide("RELIANCE", 100.0)

    # gap = 0.01%, below MIN_CROSSOVER_GAP_PCT (0.02%)
    _patch_indicators(monkeypatch, short_ma=100.01, long_ma=100.0, rsi=55)
    order = strategy.decide("RELIANCE", 100.0)

    assert order is None
    assert strategy._get_state("RELIANCE").in_position is False


def test_buy_fires_when_crossover_gap_at_or_above_min_threshold(monkeypatch):
    strategy = MARsiStrategy()

    _patch_indicators(monkeypatch, short_ma=100.0, long_ma=100.0, rsi=50)
    strategy.decide("RELIANCE", 100.0)

    # gap = 0.03%, at/above MIN_CROSSOVER_GAP_PCT (0.02%)
    _patch_indicators(monkeypatch, short_ma=100.03, long_ma=100.0, rsi=55)
    order = strategy.decide("RELIANCE", 100.0)

    assert order is not None
    assert order.side == Side.BUY


def test_no_sell_via_crossover_when_gap_below_min_threshold(monkeypatch):
    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)

    # gap = 0.01%, below threshold; RSI healthy, price above stop-loss
    _patch_indicators(monkeypatch, short_ma=100.0, long_ma=100.01, rsi=50)
    order = strategy.decide("RELIANCE", 99.5)

    assert order is None
    assert strategy._get_state("RELIANCE").in_position is True


# --- cooldown after exit ------------------------------------------------

def _patch_clock(monkeypatch, fake_clock):
    monkeypatch.setattr(mod.time, "monotonic", lambda: fake_clock["now"])


def test_reentry_blocked_within_cooldown_after_exit(monkeypatch):
    fake_clock = {"now": 0.0}
    _patch_clock(monkeypatch, fake_clock)

    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)

    _patch_indicators(monkeypatch, short_ma=95, long_ma=100, rsi=60)
    exit_order = strategy.decide("RELIANCE", 99.0)
    assert exit_order is not None and exit_order.side == Side.SELL

    fake_clock["now"] = 10.0  # 10s later, < COOLDOWN_SECONDS (60)
    _patch_indicators(monkeypatch, short_ma=90, long_ma=100, rsi=50)
    strategy.decide("RELIANCE", 100.0)
    _patch_indicators(monkeypatch, short_ma=110, long_ma=100, rsi=55)
    order = strategy.decide("RELIANCE", 100.0)

    assert order is None
    assert strategy._get_state("RELIANCE").in_position is False


def test_reentry_allowed_after_cooldown_elapses(monkeypatch):
    fake_clock = {"now": 0.0}
    _patch_clock(monkeypatch, fake_clock)

    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)

    _patch_indicators(monkeypatch, short_ma=95, long_ma=100, rsi=60)
    exit_order = strategy.decide("RELIANCE", 99.0)
    assert exit_order is not None and exit_order.side == Side.SELL

    fake_clock["now"] = 61.0  # past COOLDOWN_SECONDS (60)
    _patch_indicators(monkeypatch, short_ma=90, long_ma=100, rsi=50)
    strategy.decide("RELIANCE", 100.0)
    _patch_indicators(monkeypatch, short_ma=110, long_ma=100, rsi=55)
    order = strategy.decide("RELIANCE", 100.0)

    assert order is not None
    assert order.side == Side.BUY


def test_exit_signal_ignores_cooldown(monkeypatch):
    fake_clock = {"now": 0.0}
    _patch_clock(monkeypatch, fake_clock)

    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)

    fake_clock["now"] = 5.0  # cooldown would be active for entries, irrelevant to exits
    _patch_indicators(monkeypatch, short_ma=95, long_ma=100, rsi=60)
    order = strategy.decide("RELIANCE", 99.0)

    assert order is not None
    assert order.side == Side.SELL
    assert strategy._get_state("RELIANCE").in_position is False


# --- restore_position (restart recovery) --------------------------------

def test_restore_position_sets_in_position_and_entry_price():
    strategy = MARsiStrategy()
    strategy.restore_position("RELIANCE", 150.0)

    state = strategy._get_state("RELIANCE")
    assert state.in_position is True
    assert state.entry_price == 150.0


# --- force_exit (MIS auto-square-off resync) -----------------------------

def test_force_exit_resets_in_position_and_entry_price(monkeypatch):
    fake_clock = {"now": 123.0}
    _patch_clock(monkeypatch, fake_clock)

    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)

    strategy.force_exit("RELIANCE")

    state = strategy._get_state("RELIANCE")
    assert state.in_position is False
    assert state.entry_price is None
    assert state.last_exit_time == 123.0


def test_force_exit_leaves_ma_history_untouched(monkeypatch):
    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)
    state = strategy._get_state("RELIANCE")
    prev_short, prev_long = state.prev_short_ma, state.prev_long_ma

    strategy.force_exit("RELIANCE")

    assert state.prev_short_ma == prev_short
    assert state.prev_long_ma == prev_long


def test_force_exit_on_unknown_symbol_does_not_crash():
    strategy = MARsiStrategy()
    strategy.force_exit("TCS")  # no prior state — should lazily create and reset cleanly

    state = strategy._get_state("TCS")
    assert state.in_position is False
    assert state.entry_price is None


# --- MARsiParams override (scripts/nightly_optimize.py's sweep mechanism) ----

def test_default_params_match_module_constants():
    strategy = MARsiStrategy()
    assert strategy.params == MARsiParams(
        short_window=mod.SHORT_WINDOW, long_window=mod.LONG_WINDOW,
        rsi_window=mod.RSI_WINDOW, rsi_entry_min=mod.RSI_ENTRY_MIN,
        rsi_entry_max=mod.RSI_ENTRY_MAX, rsi_exit_overbought=mod.RSI_EXIT_OVERBOUGHT,
        stop_loss_pct=mod.STOP_LOSS_PCT, min_crossover_gap_pct=mod.MIN_CROSSOVER_GAP_PCT,
        cooldown_seconds=mod.COOLDOWN_SECONDS,
        candle_interval=mod.CANDLE_INTERVAL, candle_lookback_bars=mod.CANDLE_LOOKBACK_BARS,
    )


def test_overridden_params_change_warmup_length():
    """A smaller long_window should let the strategy fully warm up on far fewer candles than
    the production default (21) requires — proves decide() is actually reading self.params
    for its indicator windows, not the module constants."""
    candles = [{"close": p} for p in [100.0, 101.0, 99.0, 102.0]]

    short_params = MARsiParams(short_window=2, long_window=3, rsi_window=2)
    short_strategy = MARsiStrategy(params=short_params)
    short_strategy.update_candles("RELIANCE", candles)
    short_strategy.decide("RELIANCE", candles[-1]["close"])
    short_state = short_strategy._get_state("RELIANCE")
    assert short_state.prev_short_ma is not None
    assert short_state.prev_long_ma is not None

    default_strategy = MARsiStrategy()  # long_window=21 by default
    default_strategy.update_candles("RELIANCE", candles)
    default_strategy.decide("RELIANCE", candles[-1]["close"])
    default_state = default_strategy._get_state("RELIANCE")
    assert default_state.prev_long_ma is None  # still warming up with only 4 candles


def test_overridden_stop_loss_pct_changes_exit_reason_threshold():
    tight_stop = MARsiParams(stop_loss_pct=0.5)
    strategy = MARsiStrategy(params=tight_stop)
    assert strategy.params.stop_loss_pct == 0.5
    assert strategy.params.stop_loss_pct != MARsiParams().stop_loss_pct


# --- get_debug_info (dashboard "Strategy signals" card) -------------------

def test_get_debug_info_before_any_price_shows_zero_progress():
    strategy = MARsiStrategy()
    info = strategy.get_debug_info("RELIANCE")

    assert info["prices_collected"] == 0
    assert info["warmed_up"] is False
    assert info["short_ma"] is None
    assert info["in_position"] is False


def test_get_debug_info_while_warming_up_reports_progress():
    strategy = MARsiStrategy()
    strategy.update_candles("RELIANCE", [{"close": 100.0}, {"close": 101.0}])

    info = strategy.get_debug_info("RELIANCE")

    assert info["prices_collected"] == 2
    assert info["warmed_up"] is False
    assert info["prices_needed"] == max(mod.SHORT_WINDOW, mod.LONG_WINDOW, mod.RSI_WINDOW + 1)


def test_get_debug_info_once_warmed_up_reports_real_indicator_values():
    small = MARsiParams(short_window=2, long_window=3, rsi_window=2)
    strategy = MARsiStrategy(params=small)
    strategy.update_candles("RELIANCE", [{"close": p} for p in [100.0, 101.0, 102.0]])

    info = strategy.get_debug_info("RELIANCE")

    assert info["warmed_up"] is True
    assert info["short_ma"] is not None
    assert info["long_ma"] is not None
    assert info["rsi"] is not None
    assert info["gap_pct"] is not None


def test_get_debug_info_reports_in_position_after_restore():
    strategy = MARsiStrategy()
    strategy.restore_position("RELIANCE", entry_price=150.0)

    info = strategy.get_debug_info("RELIANCE")

    assert info["in_position"] is True
    assert info["entry_price"] == 150.0


def test_get_debug_info_reports_cooldown_remaining(monkeypatch):
    fake_clock = {"now": 0.0}
    _patch_clock(monkeypatch, fake_clock)
    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)
    _patch_indicators(monkeypatch, short_ma=95, long_ma=100, rsi=60)
    strategy.decide("RELIANCE", 99.0)  # exits, starts cooldown at t=0

    fake_clock["now"] = 10.0  # 10s into a 60s cooldown
    info = strategy.get_debug_info("RELIANCE")

    assert info["in_position"] is False
    assert info["cooldown_remaining_sec"] == mod.COOLDOWN_SECONDS - 10.0


def test_get_debug_info_default_returns_empty_dict_for_base_strategy():
    from strategies.base_strategy import NoOpStrategy
    assert NoOpStrategy().get_debug_info("RELIANCE") == {}


def test_restore_position_leaves_ma_history_at_defaults():
    strategy = MARsiStrategy()
    strategy.restore_position("RELIANCE", 150.0)

    state = strategy._get_state("RELIANCE")
    assert state.prev_short_ma is None
    assert state.prev_long_ma is None
    assert state.last_exit_time is None


# --- update_candles / get_candle_requirements (candle-based signal redesign) ---

def test_update_candles_replaces_not_appends():
    strategy = MARsiStrategy()
    strategy.update_candles("RELIANCE", [{"close": 100.0}, {"close": 101.0}])
    strategy.update_candles("RELIANCE", [{"close": 200.0}])

    state = strategy._get_state("RELIANCE")
    assert list(state.prices) == [200.0]


def test_update_candles_empty_list_is_a_noop():
    strategy = MARsiStrategy()
    strategy.update_candles("RELIANCE", [{"close": 100.0}, {"close": 101.0}])
    strategy.update_candles("RELIANCE", [])

    state = strategy._get_state("RELIANCE")
    assert list(state.prices) == [100.0, 101.0]


def test_decide_no_longer_mutates_prices():
    strategy = MARsiStrategy()
    strategy.decide("RELIANCE", 100.0)
    strategy.decide("RELIANCE", 101.0)
    strategy.decide("RELIANCE", 102.0)

    state = strategy._get_state("RELIANCE")
    assert len(state.prices) == 0


def test_decide_stop_loss_independent_of_candle_series(monkeypatch):
    """Stop-loss must fire from last_traded_price alone — proven here by triggering it
    without ever calling update_candles(), so no candle-derived series feeds it at all."""
    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)

    _patch_indicators(monkeypatch, short_ma=120, long_ma=100, rsi=50)
    order = strategy.decide("RELIANCE", 97.5)  # <= 100 * 0.98

    assert order is not None
    assert order.side == Side.SELL
    assert "Stop-loss hit" in order.reason
    assert len(strategy._get_state("RELIANCE").prices) == 0  # candle series never populated


def test_get_candle_requirements_returns_params_interval_and_lookback():
    params = MARsiParams(candle_interval="15minute", candle_lookback_bars=40)
    strategy = MARsiStrategy(params=params)
    assert strategy.get_candle_requirements() == ("15minute", 40)


def test_get_candle_requirements_default_base_strategy_returns_none():
    from strategies.base_strategy import NoOpStrategy
    assert NoOpStrategy().get_candle_requirements() is None
