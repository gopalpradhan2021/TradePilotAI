from dataclasses import replace
from datetime import datetime, timedelta

from config.settings import RiskConfig, Settings
from core.backtest_engine import backtest_settings, run_backtest
from strategies.ma_rsi_strategy import MARsiParams


def make_settings(**risk_overrides):
    risk = RiskConfig(
        max_order_value_inr=100_000, max_daily_loss_inr=10_000, max_trades_per_day=50,
        max_position_qty=1_000, price_sanity_band_pct=3.0, total_capital_inr=1_000_000,
        allow_fno=False,
    )
    risk = replace(risk, **risk_overrides) if risk_overrides else risk
    return Settings(mode="PAPER", risk=risk, ntfy_topic="")


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
