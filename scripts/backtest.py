"""
Backtest / market-replay runner.

Drives the real, unmodified Strategy -> RiskManager -> PaperBroker ->
Orchestrator pipeline against Groww's own historical candle data instead of
live prices — "backtest" and "market replay" are deliberately the same
engine here, not two separate simulators, so a clean backtest run is
evidence the actual production code path (not a reimplementation of it)
behaves correctly. This script can NEVER place a real order: it only ever
constructs a PaperBroker, never a LiveBroker.

Runs against an isolated scratch SQLite database and heartbeat file (auto
temp dir, deleted afterward unless --keep-db is passed) so it never touches
the real bot's data/trading.db or logs/heartbeat.json.

Requires real Groww credentials (GROWW_API_KEY/GROWW_API_SECRET in .env) to
fetch historical candles — this is a read-only API call, not trading.

Known limitations (v1):
  - DB row timestamps (created_at/opened_at/closed_at) are the backtest
    process's real wall-clock time, not the simulated historical time —
    only the P&L/win-rate/order-sequence numbers reflect history, not the
    literal recorded timestamps.
  - Multi-symbol runs use the first symbol's candle timeline to drive the
    simulated clock (day rollover, cooldown); only sensible when all
    symbols share the same session/timeframe.
  - No slippage, spread, brokerage, or partial fills — same simplified fill
    model PaperBroker already uses live (fill at that bar's close).

Usage:
    python -m scripts.backtest --symbols RELIANCE --start 2026-06-01 --end 2026-08-01 --interval 5minute
"""
import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from datetime import date, datetime


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["RELIANCE"])
    parser.add_argument("--start", required=True, help='"YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"')
    parser.add_argument("--end", required=True, help='"YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"')
    parser.add_argument("--interval", default="5minute",
                         help='Groww candle interval, e.g. "1minute", "5minute", "1day".')
    parser.add_argument("--keep-db", action="store_true",
                         help="Don't delete the scratch database after the run; path is printed.")
    parser.add_argument("--out", help="Optional path to write the report as JSON.")
    return parser.parse_args()


def _normalize_dt(value: str) -> str:
    return value if " " in value else f"{value} 00:00:00"


class SimClock:
    """Drives RiskManager's day-rollover and MARsiStrategy's cooldown off simulated
    historical time instead of the backtest process's real (much faster) run time."""

    def __init__(self, start: datetime):
        self.current = start

    def today(self) -> date:
        return self.current.date()

    def monotonic(self) -> float:
        return self.current.timestamp()


def main():
    from dotenv import load_dotenv
    load_dotenv()
    args = _parse_args()

    scratch_dir = tempfile.mkdtemp(prefix="tradepilot_backtest_")
    os.environ["DATABASE_PATH"] = os.path.join(scratch_dir, "trading.db")
    os.environ["HEARTBEAT_PATH"] = os.path.join(scratch_dir, "heartbeat.json")

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(name)s | %(message)s")

    from config.settings import load_settings
    from core.auth import get_client, GrowwAuthError
    from core.db import positions_repo, risk_repo
    from core.db.migrate import run_migrations
    from core.execution import PaperBroker
    from core.orchestrator import Orchestrator
    from core.replay_market_data import ReplayMarketDataClient
    from core.risk_manager import RiskManager
    from strategies.ma_rsi_strategy import MARsiStrategy

    run_migrations()

    try:
        client = get_client()
    except GrowwAuthError as e:
        print(f"Groww auth failed — needed to fetch historical candles (read-only): {e}")
        sys.exit(1)

    start_time = _normalize_dt(args.start)
    end_time = _normalize_dt(args.end)

    candles_by_symbol = {}
    for symbol in args.symbols:
        response = client.get_historical_candles(
            exchange=client.EXCHANGE_NSE,
            segment=client.SEGMENT_CASH,
            groww_symbol=f"NSE-{symbol}",
            start_time=start_time,
            end_time=end_time,
            candle_interval=args.interval,
        )
        candles = [
            {"timestamp": datetime.fromisoformat(row[0]), "open": row[1], "high": row[2],
             "low": row[3], "close": row[4], "volume": row[5]}
            for row in response.get("candles", [])
        ]
        candles.sort(key=lambda c: c["timestamp"])
        if not candles:
            print(f"No historical candles returned for {symbol} in that range — aborting.")
            shutil.rmtree(scratch_dir, ignore_errors=True)
            sys.exit(1)
        candles_by_symbol[symbol] = candles
        print(f"{symbol}: {len(candles)} candles, "
              f"{candles[0]['timestamp']} -> {candles[-1]['timestamp']}")

    replay_client = ReplayMarketDataClient(candles_by_symbol)
    driving_symbol = args.symbols[0]
    sim_clock = SimClock(candles_by_symbol[driving_symbol][0]["timestamp"])

    settings = replace(load_settings(), mode="BACKTEST")

    broker = PaperBroker(market_data_client=replay_client)
    risk_manager = RiskManager(settings.risk, ntfy_topic="", today_fn=sim_clock.today)
    strategy = MARsiStrategy(clock=sim_clock.monotonic)
    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    bars = 0
    while any(replay_client.has_more(s) for s in args.symbols):
        next_ts = replay_client.peek_next_timestamp(driving_symbol)
        if next_ts is not None:
            sim_clock.current = next_ts
        orchestrator.run_once(args.symbols)
        bars += 1

    closed = positions_repo.get_closed_positions()
    win_count, loss_count = positions_repo.get_win_loss_counts()
    total_trades = win_count + loss_count
    net_pnl = sum(p["realized_pnl"] for p in closed)
    wins = [p["realized_pnl"] for p in closed if p["realized_pnl"] > 0]
    losses = [p["realized_pnl"] for p in closed if p["realized_pnl"] < 0]

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for p in closed:
        equity += p["realized_pnl"]
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)

    report = {
        "symbols": args.symbols,
        "start": start_time,
        "end": end_time,
        "interval": args.interval,
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

    print("\n=== Backtest report ===")
    for k, v in report.items():
        print(f"{k}: {v}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nReport written to {args.out}")

    if args.keep_db:
        print(f"\nScratch database kept at: {os.environ['DATABASE_PATH']}")
    else:
        shutil.rmtree(scratch_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
