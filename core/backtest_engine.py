"""
Reusable core of scripts/backtest.py, factored out so scripts/nightly_optimize.py can fetch
historical candles once and replay them many times (once per candidate parameter set) without
re-fetching from Groww for every combination, and so dashboard/app.py's Backtest page can trigger
the same logic synchronously.

Deliberately still drives the real, unmodified Strategy -> RiskManager -> PaperBroker ->
Orchestrator pipeline — see scripts/backtest.py's module docstring for why. This module owns none
of the CLI/report-printing concerns; it just returns a report dict.
"""
import os
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config.settings import Settings
from core.db import positions_repo, risk_repo
from core.execution import PaperBroker
from core.orchestrator import Orchestrator
from core.replay_market_data import ReplayMarketDataClient
from core.risk_manager import RiskManager
from strategies.ma_rsi_strategy import MARsiParams, MARsiStrategy


class SimClock:
    """Drives RiskManager's day-rollover and MARsiStrategy's cooldown off simulated
    historical time instead of the calling process's real (much faster) run time."""

    def __init__(self, start: datetime):
        self.current = start

    def today(self) -> date:
        return self.current.date()

    def monotonic(self) -> float:
        return self.current.timestamp()

    def now_ist(self) -> datetime:
        """Groww's historical candle timestamps are IST local time — naive ones are
        already IST wall-clock, so they're stamped as such rather than converted."""
        if self.current.tzinfo is None:
            return self.current.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return self.current.astimezone(ZoneInfo("Asia/Kolkata"))


def run_backtest(candles_by_symbol: dict[str, list[dict]], symbols: list[str],
                  settings: Settings, strategy_params: MARsiParams | None = None) -> dict:
    """Replays pre-fetched candles (each symbol's list of {"timestamp": datetime, "close":
    float, ...} dicts, ascending) through the real pipeline, against an isolated scratch DB
    that must already be pointed at via the DATABASE_PATH/HEARTBEAT_PATH env vars (see
    scripts/backtest.py and scripts/nightly_optimize.py for how each sets those up — this
    function itself doesn't manage scratch-file lifecycle, so callers doing many runs back to
    back can choose to reuse or recreate the DB between calls)."""
    replay_client = ReplayMarketDataClient(candles_by_symbol)
    driving_symbol = symbols[0]
    sim_clock = SimClock(candles_by_symbol[driving_symbol][0]["timestamp"])

    broker = PaperBroker(market_data_client=replay_client)
    risk_manager = RiskManager(settings.risk, ntfy_topic="", today_fn=sim_clock.today,
                                now_ist_fn=sim_clock.now_ist)
    strategy = MARsiStrategy(clock=sim_clock.monotonic, params=strategy_params)
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy,
                                 clock=sim_clock.monotonic, now_ist_fn=sim_clock.now_ist)

    bars = 0
    while any(replay_client.has_more(s) for s in symbols):
        next_ts = replay_client.peek_next_timestamp(driving_symbol)
        if next_ts is not None:
            sim_clock.current = next_ts
        orchestrator.run_once(symbols)
        bars += 1

    closed = positions_repo.get_closed_positions()
    win_count, loss_count = positions_repo.get_win_loss_counts()
    total_trades = win_count + loss_count
    net_pnl = float(sum(p["realized_pnl"] for p in closed))
    wins = [p["realized_pnl"] for p in closed if p["realized_pnl"] > 0]
    losses = [p["realized_pnl"] for p in closed if p["realized_pnl"] < 0]

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for p in closed:
        equity += p["realized_pnl"]
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)

    return {
        "symbols": symbols,
        "bars_processed": bars,
        "total_trades": total_trades,
        "wins": win_count,
        "losses": loss_count,
        "win_rate_pct": round(100 * win_count / total_trades, 1) if total_trades else None,
        "net_pnl": round(net_pnl, 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "max_drawdown": round(max_drawdown, 2),
        "risk_halt_events": risk_repo.count_events("HALTED"),
    }


def backtest_settings(base_settings: Settings) -> Settings:
    """mode="BACKTEST" so it's visually distinguishable from real PAPER/LIVE runs; ntfy_topic=""
    so Orchestrator._notify() (fill/non-fill notifications) can't fire a real ntfy.sh push during
    a replay — RiskManager is separately constructed with ntfy_topic="" in run_backtest() for the
    same reason, but Orchestrator reads straight from settings.ntfy_topic, not RiskManager's."""
    return replace(base_settings, mode="BACKTEST", ntfy_topic="")


def point_db_at_scratch(db_path: str, heartbeat_path: str) -> None:
    """Sets DATABASE_PATH/HEARTBEAT_PATH for the current process. Must be called before any
    core.db.* or core.status_writer call — both read these env vars fresh on every call, so
    this can be called (and re-called with a new path) at any point before that, not just at
    process startup."""
    os.environ["DATABASE_PATH"] = db_path
    os.environ["HEARTBEAT_PATH"] = heartbeat_path
