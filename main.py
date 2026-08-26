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
from core.execution import PaperBroker, LiveBroker, BrokerPositionFetchError
from core.notifier import send_notification
from core.risk_manager import RiskManager
from core.orchestrator import Orchestrator
from strategies.ma_rsi_strategy import MARsiStrategy


def reconcile_positions(broker, risk_manager, symbols: list[str], logger) -> None:
    """Startup-only, LIVE-mode-only check: compares the local DB's open positions against
    what the broker actually reports, and halts (AUTO — can't be casually resumed) on any
    mismatch or fetch failure, since a divergence here means the local ledger can't be
    trusted. Not run periodically during run_forever() — that's a larger, deliberately
    deferred scope for a bot with no live date yet."""
    for symbol in symbols:
        local_pos = positions_repo.get_open_position(symbol)
        local_qty = local_pos["qty"] if local_pos else 0
        try:
            broker_qty = broker.get_broker_position(symbol)["qty"]
        except BrokerPositionFetchError as e:
            reason = f"Reconciliation: could not fetch broker position for {symbol}: {e}"
            logger.error(reason)
            risk_manager.halt_reconciliation_mismatch(reason)
            continue
        if local_qty != broker_qty:
            reason = (
                f"Position mismatch at startup for {symbol}: "
                f"local DB shows {local_qty}, broker reports {broker_qty}."
            )
            logger.critical("RECONCILIATION MISMATCH: %s", reason)
            risk_manager.halt_reconciliation_mismatch(reason)


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

    risk_manager = RiskManager(settings.risk, ntfy_topic=settings.ntfy_topic)

    if want_live:
        logger.warning("Starting in LIVE mode — real orders will be placed.")
        try:
            groww_client = get_client()
        except GrowwAuthError as e:
            logger.error("Cannot start live trading — auth failed: %s", e)
            sys.exit(1)
        broker = LiveBroker(groww_client)
        reconcile_positions(broker, risk_manager, args.symbols, logger)
    else:
        logger.info("Starting in PAPER mode — no real orders will be placed.")
        market_data_client = None
        try:
            market_data_client = get_client()
        except GrowwAuthError:
            logger.warning("No Groww credentials found — paper mode will run without live LTP.")
        broker = PaperBroker(market_data_client=market_data_client)

    strategy = MARsiStrategy()

    for symbol in args.symbols:
        open_pos = positions_repo.get_open_position(symbol)
        if open_pos is not None:
            strategy.restore_position(symbol, entry_price=open_pos["entry_price"])
            logger.info("Restored open position for %s @ entry %.2f", symbol, open_pos["entry_price"])

    orchestrator = Orchestrator(settings, broker, risk_manager, strategy)

    try:
        send_notification(settings, f"🟢 groww-bot started | mode={settings.mode} | symbols={args.symbols}")
    except Exception as e:
        logger.error("Failed to send startup notification: %s", e)

    orchestrator.run_forever(symbols=args.symbols, poll_interval_sec=args.poll_interval)


if __name__ == "__main__":
    main()
