import strategies.ma_rsi_strategy as mod
from core.models import Side
from strategies.ma_rsi_strategy import MARsiStrategy, _sma, _rsi


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

    # gap = 0.03%, below MIN_CROSSOVER_GAP_PCT (0.05%)
    _patch_indicators(monkeypatch, short_ma=100.03, long_ma=100.0, rsi=55)
    order = strategy.decide("RELIANCE", 100.0)

    assert order is None
    assert strategy._get_state("RELIANCE").in_position is False


def test_buy_fires_when_crossover_gap_at_or_above_min_threshold(monkeypatch):
    strategy = MARsiStrategy()

    _patch_indicators(monkeypatch, short_ma=100.0, long_ma=100.0, rsi=50)
    strategy.decide("RELIANCE", 100.0)

    # gap = 0.06%, at/above MIN_CROSSOVER_GAP_PCT (0.05%)
    _patch_indicators(monkeypatch, short_ma=100.06, long_ma=100.0, rsi=55)
    order = strategy.decide("RELIANCE", 100.0)

    assert order is not None
    assert order.side == Side.BUY


def test_no_sell_via_crossover_when_gap_below_min_threshold(monkeypatch):
    strategy = MARsiStrategy()
    _enter_position(strategy, monkeypatch, entry_price=100.0)

    # gap = 0.03%, below threshold; RSI healthy, price above stop-loss
    _patch_indicators(monkeypatch, short_ma=100.0, long_ma=100.03, rsi=50)
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


def test_restore_position_leaves_ma_history_at_defaults():
    strategy = MARsiStrategy()
    strategy.restore_position("RELIANCE", 150.0)

    state = strategy._get_state("RELIANCE")
    assert state.prev_short_ma is None
    assert state.prev_long_ma is None
    assert state.last_exit_time is None
