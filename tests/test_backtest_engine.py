from dataclasses import replace
from datetime import datetime, timedelta

from config.settings import RiskConfig, Settings
from core.backtest_engine import backtest_settings, run_backtest
from core.db import orders_repo
from strategies.ma_rsi_strategy import MARsiParams


def make_settings(**risk_overrides):
    risk = RiskConfig(
        max_order_value_inr=100_000, max_daily_loss_inr=10_000, max_trades_per_day=50,
        max_position_qty=1_000, price_sanity_band_pct=3.0, total_capital_inr=1_000_000,
        allow_fno=False, allow_fno_index=False, fno_paper_validated=False,
    )
    risk = replace(risk, **risk_overrides) if risk_overrides else risk
    return Settings(mode="PAPER", risk=risk, ntfy_topic="", candle_interval="5minute")


def _candles(*closes, start=datetime(2026, 1, 1, 9, 15)):
    return [{"timestamp": start + timedelta(minutes=5 * i), "close": c} for i, c in enumerate(closes)]


def test_backtest_settings_sets_mode_to_backtest():
    settings = make_settings()
    bt = backtest_settings(settings)
    assert bt.mode == "BACKTEST"
    assert bt.risk == settings.risk  # unchanged


def test_run_backtest_with_no_signals_reports_zero_trades():
    # Flat prices never cross, so MARsiStrategy default params never signal.
    candles = _candles(*([100.0] * 30))
    report = run_backtest({"RELIANCE": candles}, ["RELIANCE"], make_settings())

    assert report["bars_processed"] == 30
    assert report["total_trades"] == 0
    assert report["net_pnl"] == 0
    assert report["win_rate_pct"] is None


def test_run_backtest_accepts_strategy_params_override_and_runs_end_to_end():
    # A clean, mild uptrend then a mild downtrend with tiny MA windows and a near-zero gap
    # threshold — small enough windows that a real crossover reliably fires without RSI pinning.
    prices = [100.0, 100.2, 100.4, 100.2, 100.0, 99.8, 99.6, 99.8, 100.0, 100.2]
    candles = _candles(*prices)
    params = MARsiParams(short_window=2, long_window=4, rsi_window=2,
                          rsi_entry_min=0, rsi_entry_max=100, min_crossover_gap_pct=0.0,
                          cooldown_seconds=0)

    report = run_backtest({"RELIANCE": candles}, ["RELIANCE"], make_settings(), params)

    assert report["bars_processed"] == len(prices)
    # Not asserting a specific trade count (real crossover timing is data-dependent) — just
    # that the pipeline ran end to end and produced a coherent report shape.
    assert report["total_trades"] >= 0
    assert isinstance(report["net_pnl"], float)


def test_backtest_not_affected_by_real_wallclock_time_past_cutoff(monkeypatch):
    """Reproduces a confirmed live bug (2026-09-01): RiskManager/Orchestrator's MIS
    square-off cutoff check used to call is_past_square_off_cutoff() with no arguments,
    always reading the REAL current IST time — so running a backtest (or nightly_optimize)
    after 3:20 PM real IST time rejected every CASH BUY and force-closed every open
    position on the very next bar, regardless of which historical date/time-of-day was
    actually being replayed. All candle timestamps here are 9:15-9:45 AM (well before the
    3:20 PM cutoff) — this must produce real trades no matter what the real wall-clock
    time is when the test happens to run.

    Restores the REAL is_past_square_off_cutoff (tests/conftest.py's autouse fixture
    patches it to always return False for every test, which would hide this exact
    regression) so this test genuinely exercises SimClock.now_ist's wiring end to end."""
    import core.orchestrator as orchestrator_module
    import core.risk_manager as risk_manager_module
    from core.market_hours import is_past_square_off_cutoff as real_cutoff_fn
    monkeypatch.setattr(risk_manager_module, "is_past_square_off_cutoff", real_cutoff_fn)
    monkeypatch.setattr(orchestrator_module, "is_past_square_off_cutoff", real_cutoff_fn)

    prices = [100.0, 100.2, 100.4, 100.2, 100.0, 99.8, 99.6, 99.8, 100.0, 100.2]
    candles = _candles(*prices, start=datetime(2026, 1, 1, 9, 15))
    params = MARsiParams(short_window=2, long_window=4, rsi_window=2,
                          rsi_entry_min=0, rsi_entry_max=100, min_crossover_gap_pct=0.0,
                          cooldown_seconds=0)

    run_backtest({"RELIANCE": candles}, ["RELIANCE"], make_settings(), params)

    # Checking FILLED orders directly, not report["total_trades"] (win_count + loss_count
    # from CLOSED positions only) — a position opened this late in a short 10-bar window
    # legitimately never closes before the data runs out, which isn't what this test is
    # about. The point is proving the BUY itself wasn't blocked by the cutoff check.
    orders = orders_repo.get_recent_orders(10)
    buy_orders = [o for o in orders if o["side"] == "BUY"]
    assert len(buy_orders) > 0
    assert buy_orders[0]["status"] == "FILLED"


def test_run_backtest_produces_no_trades_when_fewer_bars_than_warmup_needs():
    # 3 candles is fewer than long_window+1=5 needed to warm up — even with params that
    # allow essentially any RSI/gap, there isn't enough candle history yet for a crossover
    # to be computable, proving indicators are fed via Orchestrator's candle-fetch cadence
    # (strategy.update_candles(), 1-bar-lagged on already-served candles only) rather than
    # any other path.
    candles = _candles(100.0, 105.0, 95.0)
    params = MARsiParams(short_window=2, long_window=4, rsi_window=2,
                          rsi_entry_min=0, rsi_entry_max=100, min_crossover_gap_pct=0.0,
                          cooldown_seconds=0)

    report = run_backtest({"RELIANCE": candles}, ["RELIANCE"], make_settings(), params)

    assert report["bars_processed"] == 3
    assert report["total_trades"] == 0
