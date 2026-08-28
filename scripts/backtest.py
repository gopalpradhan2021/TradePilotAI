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


def fetch_candles(client, symbol: str, start_time: str, end_time: str, interval: str) -> list[dict]:
    """Shared by this CLI and scripts/nightly_optimize.py."""
    from core.execution import _parse_candles

    response = client.get_historical_candles(
        exchange=client.EXCHANGE_NSE,
        segment=client.SEGMENT_CASH,
        groww_symbol=f"NSE-{symbol}",
        start_time=start_time,
        end_time=end_time,
        candle_interval=interval,
    )
    return _parse_candles(response)


def main():
    from dotenv import load_dotenv
    load_dotenv()
    args = _parse_args()

    scratch_dir = tempfile.mkdtemp(prefix="tradepilot_backtest_")

    from core.backtest_engine import point_db_at_scratch
    point_db_at_scratch(os.path.join(scratch_dir, "trading.db"),
                         os.path.join(scratch_dir, "heartbeat.json"))

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(name)s | %(message)s")

    from config.settings import load_settings
    from core.auth import get_client, GrowwAuthError
    from core.backtest_engine import backtest_settings, run_backtest
    from core.db.migrate import run_migrations

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
        candles = fetch_candles(client, symbol, start_time, end_time, args.interval)
        if not candles:
            print(f"No historical candles returned for {symbol} in that range — aborting.")
            shutil.rmtree(scratch_dir, ignore_errors=True)
            sys.exit(1)
        candles_by_symbol[symbol] = candles
        print(f"{symbol}: {len(candles)} candles, "
              f"{candles[0]['timestamp']} -> {candles[-1]['timestamp']}")

    settings = backtest_settings(load_settings())
    report = run_backtest(candles_by_symbol, args.symbols, settings)
    report = {"start": start_time, "end": end_time, "interval": args.interval, **report}

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
