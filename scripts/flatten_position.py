"""
Operator emergency tool: immediately close open position(s) against the REAL broker position,
bypassing RiskManager.check() entirely, then halts the bot.

Standalone and independent of the running bot process (same pattern as scripts/halt_bot.py).
Requires MODE=LIVE in .env and a typed confirmation.

Deliberately bypasses risk_manager.check(): flatten is a risk-REDUCING action, and the whole
point of an emergency exit is that it must not be blockable by the same caps that exist to stop
risk-INCREASING ones (max_trades_per_day, price sanity band, etc.) — those could easily already
be exhausted on exactly the kind of day this tool gets used. The fill is still fully recorded in
orders_repo/positions_repo, and risk_manager.record_fill() still runs afterward so daily
counters/P&L stay accurate for future decisions.

Always halts the bot after a flatten (matches the roadmap's "FLATTEN ... remain halted"), and
because the RUNNING bot process's in-memory strategy state (MARsiStrategy.SymbolState.in_position)
has no way to learn about this out-of-process close — only a restart re-syncs it via
BaseStrategy.restore_position() at startup. The halt alone does not fix that; you MUST restart
groww-bot.service before resuming, or the strategy will still think it's in a position that no
longer exists.

Uses the REAL broker-reported position as the source of truth (get_broker_position), not the
local DB, matching the roadmap's "fetch actual broker positions" guidance — if the local ledger
is wrong, flattening against it would be wrong too.

Usage:
    python -m scripts.flatten_position --symbols RELIANCE [TCS ...]
"""
import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("groww_agent.flatten_position")

CONFIRMATION_PHRASE = "FLATTEN"


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--reason", default="manual emergency flatten")
    return parser.parse_args()


def main():
    args = _parse_args()

    if os.getenv("MODE", "PAPER").upper() != "LIVE":
        print("Refusing: MODE=LIVE is not set in .env. This tool only operates against real positions.")
        sys.exit(1)

    from config.settings import load_settings
    from core.auth import get_client, GrowwAuthError
    from core.db import orders_repo, positions_repo
    from core.execution import LiveBroker, BrokerPositionFetchError
    from core.models import OrderType, ProposedOrder, Segment, Side
    from core.risk_manager import RiskManager

    try:
        client = get_client()
    except GrowwAuthError as e:
        print(f"Groww auth failed: {e}")
        sys.exit(1)

    broker = LiveBroker(client)

    to_flatten = []
    for symbol in args.symbols:
        try:
            qty = broker.get_broker_position(symbol)["qty"]
        except BrokerPositionFetchError as e:
            print(f"Could not fetch broker position for {symbol}: {e}")
            print("Refusing to guess — aborting entirely rather than flattening some symbols "
                  "on incomplete information.")
            sys.exit(1)
        if qty > 0:
            to_flatten.append((symbol, qty))
        else:
            print(f"{symbol}: broker reports flat (qty=0) — nothing to do.")

    if not to_flatten:
        print("Nothing to flatten. Exiting without halting.")
        return

    print("\nWill place REAL closing SELL orders for:")
    for symbol, qty in to_flatten:
        print(f"  {symbol}: qty={qty}")
    print("\nThis bypasses normal risk checks and will halt the bot afterward — you must "
          "restart groww-bot.service before resuming, to clear its stale in-memory strategy state.")
    typed = input(f'Type "{CONFIRMATION_PHRASE}" to proceed: ')
    if typed != CONFIRMATION_PHRASE:
        print("Confirmation phrase did not match. Aborting.")
        sys.exit(1)

    settings = load_settings()
    risk_manager = RiskManager(settings.risk, ntfy_topic=settings.ntfy_topic, mode=settings.mode)

    for symbol, qty in to_flatten:
        order = ProposedOrder(
            symbol=symbol, side=Side.SELL, qty=qty, order_type=OrderType.MARKET,
            segment=Segment.CASH, reason=f"Emergency flatten: {args.reason}",
        )
        order_id = orders_repo.insert_order(order, status="PROPOSED", reference_price=None)
        result = broker.place_order(order, last_traded_price=None)
        orders_repo.update_order_status(
            order_id, status=result.status, broker_order_id=result.broker_order_id,
            fill_price=result.fill_price, message=result.message,
        )

        if result.status == "FILLED" and result.fill_price is not None:
            local_pos = positions_repo.get_open_position(symbol)
            if local_pos is not None:
                pnl_delta = positions_repo.close_position(
                    symbol=symbol, exit_price=result.fill_price, exit_order_id=order_id,
                )
            else:
                pnl_delta = 0.0
                logger.warning(
                    "%s: flattened at the broker but local DB had no open position to close — "
                    "local ledger was already out of sync before this flatten.", symbol,
                )
            risk_manager.record_fill(
                side=Side.SELL, order_value=result.fill_price * qty,
                pnl_delta=pnl_delta, order_id=order_id,
            )
            print(f"{symbol}: FLATTENED — sold {qty} @ {result.fill_price}")
        else:
            print(f"{symbol}: order did NOT confirm as filled (status={result.status}, "
                  f"message={result.message!r}) — verify manually in the Groww app.")

    risk_manager.manual_halt(f"Emergency flatten executed: {args.reason}")
    print("\nBot HALTED. Restart groww-bot.service before resuming — the running process's "
          "in-memory strategy state does not know about this flatten until it restarts.")


if __name__ == "__main__":
    main()
