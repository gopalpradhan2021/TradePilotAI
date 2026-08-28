"""
Entry point.

Usage:
    python main.py                 -> paper trading, regardless of .env MODE
    python main.py --live          -> live trading, ONLY if MODE=LIVE in .env too

Requiring BOTH the --live flag AND MODE=LIVE in .env is intentional
redundancy: a stray env var or a stray flag alone can't put real money
on the line by accident.
"""
import argparse
import logging
import logging.handlers
import os
import sys

from config.settings import load_settings
from core.auth import get_client, GrowwAuthError
from core.db import positions_repo
from core.db.migrate import run_migrations
from core.execution import PaperBroker, LiveBroker
from core.fno_market_data import FnoMarketData
from core.margin_provider import GrowwMarginProvider
from core.notifier import send_notification
from core.reconciliation import reconcile_positions
from core.risk_manager import RiskManager
from core.orchestrator import Orchestrator
from strategies.iv_oi_strategy import IvOiStrategy
from strategies.ma_rsi_strategy import MARsiStrategy


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    handler = logging.handlers.RotatingFileHandler(
        "logs/trading.log", maxBytes=10_000_000, backupCount=10
    )
    handler.setFormatter(logging.Formatter(fmt))
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter(fmt))
    logging.basicConfig(level=logging.INFO, handlers=[handler, stream])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                         help="Enable live trading (requires MODE=LIVE in .env too).")
    parser.add_argument("--symbols", nargs="+", default=["RELIANCE"],
                         help="Trading symbols to run the strategy against.")
    parser.add_argument("--fno-underlyings", nargs="+", default=[],
                         help="Index F&O underlyings (e.g. NIFTY BANKNIFTY) to run the "
                              "IV/OI/Greeks strategy against — separate from --symbols "
                              "since it's a different instrument universe. Requires "
                              "ALLOW_FNO=true and ALLOW_FNO_INDEX=true in .env.")
    parser.add_argument("--poll-interval", type=int, default=5)
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("groww_agent.main")

    run_migrations()
    settings = load_settings()
    want_live = args.live and settings.mode == "LIVE"

    if args.live and settings.mode != "LIVE":
        logger.error(
            "Refusing to start live: --live was passed but .env MODE is '%s', not LIVE. "
            "Set MODE=LIVE in .env to confirm you intend to trade with real money.",
            settings.mode,
        )
        sys.exit(1)

    if want_live and os.getenv("BACKTEST_VALIDATED", "false").strip().lower() != "true":
        logger.error(
            "Refusing to start live: BACKTEST_VALIDATED is not 'true' in .env. Run "
            "scripts/backtest.py (backtest/market-replay against historical data) and "
            "validate the strategy against real paper-trading results first, then set "
            "BACKTEST_VALIDATED=true in .env once you're satisfied — this is a third, "
            "independent guard alongside --live and MODE=LIVE so real money can't be put "
            "at risk before that validation has actually happened."
        )
        sys.exit(1)

    if want_live and args.fno_underlyings and not settings.risk.fno_paper_validated:
        logger.error(
            "Refusing to start live F&O trading: FNO_PAPER_VALIDATED is not 'true' in "
            ".env. No historical option-chain data exists to run a real backtest against "
            "(Groww's chain API is live-snapshot only) — this is the honest substitute for "
            "BACKTEST_VALIDATED: review a stretch of PAPER-mode decide_fno() output first, "
            "then set FNO_PAPER_VALIDATED=true once satisfied. This blocks --fno-underlyings "
            "specifically; CASH-only live trading is unaffected."
        )
        sys.exit(1)

    # Fetch the Groww client up front (shared by the broker AND the margin provider) —
    # needed before risk_manager is constructed, since LIVE mode's reconcile_positions()
    # call right after broker construction already requires a fully wired risk_manager.
    groww_client = None
    if want_live:
        logger.warning("Starting in LIVE mode — real orders will be placed.")
        try:
            groww_client = get_client()
        except GrowwAuthError as e:
            logger.error("Cannot start live trading — auth failed: %s", e)
            sys.exit(1)
    else:
        logger.info("Starting in PAPER mode — no real orders will be placed.")
        try:
            groww_client = get_client()
        except GrowwAuthError:
            logger.warning("No Groww credentials found — paper mode will run without live LTP.")

    # PAPER mode gets a real margin_provider too when credentials are available — margin
    # lookups are read-only and paper-safe, so Phase B's F&O sizing can be exercised
    # against real SPAN/exposure margin without placing any real order.
    margin_provider = GrowwMarginProvider(groww_client) if groww_client is not None else None
    risk_manager = RiskManager(settings.risk, ntfy_topic=settings.ntfy_topic, mode=settings.mode,
                                margin_provider=margin_provider)

    if want_live:
        broker = LiveBroker(groww_client)
        reconcile_positions(broker, risk_manager, args.symbols, logger)
    else:
        broker = PaperBroker(market_data_client=groww_client)

    strategy = MARsiStrategy()

    for symbol in args.symbols:
        open_pos = positions_repo.get_open_position(symbol)
        if open_pos is not None:
            strategy.restore_position(symbol, entry_price=open_pos["entry_price"])
            logger.info("Restored open position for %s @ entry %.2f", symbol, open_pos["entry_price"])

    fno_strategy = None
    fno_market_data = None
    if args.fno_underlyings:
        fno_strategy = IvOiStrategy(lot_size_fn=broker.get_lot_size)
        fno_market_data = FnoMarketData(broker)
        # KNOWN GAP: FNO position restoration on restart isn't wired up. An open FNO
        # position's DB row is keyed by the specific contract's trading_symbol, not the
        # underlying, and IvOiStrategy.restore_position() can only mark in_position=True
        # without recovering which exact contract is held (see its docstring) — a real
        # restart-time reconcile for FNO needs the full contract identity from
        # positions_repo and isn't built in this pass.
        logger.info("F&O (index options) enabled for: %s", args.fno_underlyings)

    orchestrator = Orchestrator(settings, broker, risk_manager, strategy,
                                 fno_strategy=fno_strategy, fno_market_data=fno_market_data)

    try:
        send_notification(settings, f"🟢 groww-bot started | mode={settings.mode} | symbols={args.symbols}")
    except Exception as e:
        logger.error("Failed to send startup notification: %s", e)

    orchestrator.run_forever(symbols=args.symbols, poll_interval_sec=args.poll_interval,
                              fno_underlyings=args.fno_underlyings or None)


if __name__ == "__main__":
    main()
