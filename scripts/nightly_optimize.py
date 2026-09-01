"""
Nightly off-hours strategy parameter sweep.

Runs after market close (see deploy/groww-nightly-optimize.timer), fetches the longest single
candle window Groww allows for the chosen --interval, splits it into an in-sample window (search)
and an out-of-sample window (validation), and backtests a small grid of MARsiParams candidates
plus the current production defaults (the baseline) across both windows.

This NEVER changes what the live/paper bot actually trades with — it only ever writes a report
row (core/db/optimization_repo.py) and sends one notification. Adopting a candidate requires a
human to edit strategies/ma_rsi_strategy.py's defaults and redeploy; no code path here does that
automatically.

Ranking uses only out-of-sample net P&L, specifically to avoid rewarding parameter sets that just
curve-fit the search window. A candidate with fewer than MIN_TRADES_FOR_CONFIDENCE trades in
either window is flagged "insufficient sample" rather than ranked on a lucky single trade.

Defaults to daily candles (180-day lookback) — the scheduled nightly cron job's mode, thorough but
cheap since a daily-bar backtest is fast regardless of grid size. --interval 5minute switches to
real intraday candles instead (the actual granularity the live bot trades at), capped by Groww at
a ~30-day lookback (see --days) — pass --quick too unless you're prepared to wait: an intraday
backtest processes ~15x more bars than a daily one per calendar day, so the full 243-combo grid
against 15 symbols can run for hours on this droplet's single vCPU. Confirmed live 2026-09-01: a
daily-candle sweep found too few trades in any window to draw a real conclusion; the same symbols'
actual 5-minute performance turned out to be a clear net loss (106 trades, 21.7% win rate) — the
daily-bar sweep's "inconclusive" verdict was an artifact of too little data, not evidence the
strategy was fine, which is the whole reason this flag exists now.

Usage:
    python -m scripts.nightly_optimize [--symbols RELIANCE] [--force]
    python -m scripts.nightly_optimize --symbols RELIANCE --interval 5minute --days 28 --quick

--force skips the "are we actually after market close" check (for manual/debug runs).
"""
import argparse
import itertools
import logging
import os
import shutil
import sys
import tempfile

logger = logging.getLogger("groww_agent.nightly_optimize")

MIN_TRADES_FOR_CONFIDENCE = 2
TOP_N_CANDIDATES = 5
IN_SAMPLE_FRACTION = 2 / 3

# Small, explicit grid — kept intentionally modest so a nightly run on a 512MB droplet finishes
# in a reasonable time running strictly sequentially, one candidate at a time. --quick trims each
# dimension to its first 2 choices (32 combos instead of 243) — see the module docstring for why
# that matters for --interval 5minute specifically.
SHORT_WINDOW_CHOICES = [5, 9, 12]
LONG_WINDOW_CHOICES = [15, 21, 26]
RSI_BAND_CHOICES = [(35, 70), (40, 70), (40, 75)]
MIN_GAP_CHOICES = [0.0003, 0.0005, 0.001]
COOLDOWN_CHOICES = [30, 60, 120]


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["RELIANCE"])
    parser.add_argument("--force", action="store_true",
                         help="Run even if the market currently looks open (manual/debug use).")
    parser.add_argument("--interval", default="1day",
                         help='Candle interval for the sweep, e.g. "1day" (default) or "5minute".')
    parser.add_argument("--days", type=int, default=179,
                         help="Lookback window in days (default 179; Groww caps 5minute candles "
                              "at roughly 30 days — pass e.g. --days 28 for an intraday sweep).")
    parser.add_argument("--quick", action="store_true",
                         help="Trim the parameter grid to 32 combinations instead of 243 — for "
                              "slow intraday (--interval 5minute) sweeps.")
    return parser.parse_args()


def _param_grid(quick: bool = False):
    from strategies.ma_rsi_strategy import MARsiParams
    short_choices = SHORT_WINDOW_CHOICES[:2] if quick else SHORT_WINDOW_CHOICES
    long_choices = LONG_WINDOW_CHOICES[:2] if quick else LONG_WINDOW_CHOICES
    rsi_choices = RSI_BAND_CHOICES[:2] if quick else RSI_BAND_CHOICES
    gap_choices = MIN_GAP_CHOICES[:2] if quick else MIN_GAP_CHOICES
    cooldown_choices = COOLDOWN_CHOICES[:2] if quick else COOLDOWN_CHOICES
    for short_w, long_w, rsi_band, gap, cooldown in itertools.product(
        short_choices, long_choices, rsi_choices, gap_choices, cooldown_choices,
    ):
        if long_w <= short_w:
            continue
        yield MARsiParams(
            short_window=short_w, long_window=long_w,
            rsi_entry_min=rsi_band[0], rsi_entry_max=rsi_band[1],
            min_crossover_gap_pct=gap, cooldown_seconds=cooldown,
        )


def _run_one(candles_by_symbol, symbols, settings, strategy_params) -> dict:
    """Each call gets a genuinely fresh scratch DB — reusing one across calls would let a
    prior candidate's positions/orders/risk state leak into the next candidate's result."""
    from core.backtest_engine import point_db_at_scratch, run_backtest
    from core.db.migrate import run_migrations

    scratch_dir = tempfile.mkdtemp(prefix="tradepilot_optimize_")
    try:
        point_db_at_scratch(os.path.join(scratch_dir, "trading.db"),
                             os.path.join(scratch_dir, "heartbeat.json"))
        run_migrations()
        return run_backtest(candles_by_symbol, symbols, settings, strategy_params)
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def _params_to_dict(params) -> dict:
    from dataclasses import asdict
    return asdict(params)


def rank_candidates(candidate_results: list[tuple]) -> list[dict]:
    """candidate_results: [(MARsiParams, in_sample_report, out_sample_report), ...] — already
    restricted to candidates whose in-sample net_pnl was positive (main() does that filtering
    before calling this, since it needs to decide whether to even run the out-of-sample backtest).
    Ranks by out-of-sample net_pnl only. Pure function — no I/O — so it's directly unit-testable."""
    scored = []
    for params, in_report, out_report in candidate_results:
        insufficient = (
            in_report["total_trades"] < MIN_TRADES_FOR_CONFIDENCE
            or out_report["total_trades"] < MIN_TRADES_FOR_CONFIDENCE
        )
        scored.append({
            "params": _params_to_dict(params),
            "in_sample": in_report,
            "out_sample": out_report,
            "insufficient_sample": insufficient,
        })

    scored.sort(key=lambda c: c["out_sample"]["net_pnl"], reverse=True)
    return scored[:TOP_N_CANDIDATES]


def main():
    from dotenv import load_dotenv
    load_dotenv()
    args = _parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    from core.market_hours import market_status_ist
    state, _, _ = market_status_ist()
    if state == "OPEN" and not args.force:
        logger.warning("Market looks OPEN — refusing to run (pass --force to override). "
                        "This job is meant for off-hours only.")
        sys.exit(1)

    from config.settings import load_settings
    from core.auth import get_client, GrowwAuthError
    from core.backtest_engine import backtest_settings
    from core.db import optimization_repo
    from core.notifier import send_notification_raw
    from scripts.backtest import fetch_candles
    from strategies.ma_rsi_strategy import MARsiParams

    try:
        client = get_client()
    except GrowwAuthError as e:
        logger.error("Groww auth failed — needed to fetch historical candles (read-only): %s", e)
        sys.exit(1)

    settings = load_settings()
    base_settings = backtest_settings(settings)
    ntfy_topic = settings.ntfy_topic

    # _run_one() below repeatedly repoints DATABASE_PATH/HEARTBEAT_PATH at a scratch dir it
    # deletes afterward — capture the real production paths now (before anything mutates them)
    # and make sure the real DB actually has today's migrations, since we write the final
    # optimization_runs row back to the real DB, not a scratch one.
    real_db_path = os.getenv("DATABASE_PATH", "data/trading.db")
    real_heartbeat_path = os.getenv("HEARTBEAT_PATH", "logs/heartbeat.json")
    from core.db.migrate import run_migrations as run_real_migrations
    run_real_migrations()

    from datetime import date, timedelta
    end = date.today()
    start = end - timedelta(days=args.days)
    start_str, end_str = f"{start} 00:00:00", f"{end} 23:59:59"

    for symbol in args.symbols:
        logger.info("Fetching %d-day %s candles for %s", args.days, args.interval, symbol)
        candles = fetch_candles(client, symbol, start_str, end_str, args.interval)
        if len(candles) < 30:
            logger.warning("%s: only %d candles returned — skipping (too little data).",
                            symbol, len(candles))
            continue

        split_idx = int(len(candles) * IN_SAMPLE_FRACTION)
        in_sample = candles[:split_idx]
        out_sample = candles[split_idx:]
        logger.info("%s: %d candles total, in-sample %s->%s (%d bars), "
                    "out-of-sample %s->%s (%d bars)",
                    symbol, len(candles),
                    in_sample[0]["timestamp"], in_sample[-1]["timestamp"], len(in_sample),
                    out_sample[0]["timestamp"], out_sample[-1]["timestamp"], len(out_sample))

        baseline_params = MARsiParams()
        baseline_in = _run_one({symbol: in_sample}, [symbol], base_settings, baseline_params)
        baseline_out = _run_one({symbol: out_sample}, [symbol], base_settings, baseline_params)
        baseline = {
            "params": _params_to_dict(baseline_params),
            "in_sample": baseline_in,
            "out_sample": baseline_out,
        }
        logger.info("%s baseline: in-sample net_pnl=%s trades=%s | out-of-sample net_pnl=%s trades=%s",
                    symbol, baseline_in["net_pnl"], baseline_in["total_trades"],
                    baseline_out["net_pnl"], baseline_out["total_trades"])

        candidate_results = []
        combos_tried = 0
        for params in _param_grid(args.quick):
            combos_tried += 1
            in_report = _run_one({symbol: in_sample}, [symbol], base_settings, params)
            if in_report["total_trades"] == 0 or in_report["net_pnl"] <= 0:
                continue
            out_report = _run_one({symbol: out_sample}, [symbol], base_settings, params)
            candidate_results.append((params, in_report, out_report))

        logger.info("%s: tried %d combinations, %d cleared the in-sample filter",
                    symbol, combos_tried, len(candidate_results))

        top_candidates = rank_candidates(candidate_results)

        best_beats_baseline = (
            top_candidates
            and not top_candidates[0]["insufficient_sample"]
            and top_candidates[0]["out_sample"]["net_pnl"] > baseline_out["net_pnl"]
        )

        # Point back at the real DB before writing the result — every _run_one() call above
        # left DATABASE_PATH pointed at a (now-deleted) scratch dir.
        os.environ["DATABASE_PATH"] = real_db_path
        os.environ["HEARTBEAT_PATH"] = real_heartbeat_path

        optimization_repo.record_run(
            symbol=symbol,
            in_sample_start=str(in_sample[0]["timestamp"]),
            in_sample_end=str(in_sample[-1]["timestamp"]),
            out_sample_start=str(out_sample[0]["timestamp"]),
            out_sample_end=str(out_sample[-1]["timestamp"]),
            baseline=baseline,
            candidates=top_candidates,
            combinations_tried=combos_tried,
            notes="candidate beats baseline out-of-sample" if best_beats_baseline else "",
        )

        if best_beats_baseline:
            best = top_candidates[0]
            msg = (
                f"📊 Nightly optimization ({symbol}): a candidate parameter set beat baseline "
                f"out-of-sample (₹{best['out_sample']['net_pnl']:.2f} vs baseline "
                f"₹{baseline_out['net_pnl']:.2f}, {best['out_sample']['total_trades']} trades). "
                f"Review on the dashboard's Backtest page before considering adoption — nothing "
                f"has been changed automatically."
            )
        else:
            msg = (
                f"📊 Nightly optimization ({symbol}): no candidate beat the current baseline "
                f"out-of-sample tonight. No action needed."
            )
        try:
            send_notification_raw(ntfy_topic, msg)
        except Exception as e:
            logger.error("Failed to send notification: %s", e)

        logger.info(msg)


if __name__ == "__main__":
    main()
