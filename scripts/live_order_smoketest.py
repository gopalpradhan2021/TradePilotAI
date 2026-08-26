"""
One-off manual smoke test for LiveBroker against the real Groww API.

This is NOT part of the trading loop and deliberately does not go through
Orchestrator/RiskManager/orders_repo — it exists solely to answer the question
"does LiveBroker.place_order() actually work against real Groww", which has
never been verified (see CLAUDE.md Phase 4 notes). Because it bypasses the
risk gate, it enforces its own hardcoded, non-configurable safety caps below
instead of trusting .env risk knobs.

This places a REAL order with REAL money. It will refuse to run unless:
  - MODE=LIVE is set in .env (same redundant guard main.py uses)
  - --confirm is passed on the command line
  - you type the exact confirmation phrase when prompted

Usage:
    python -m scripts.live_order_smoketest --symbol RELIANCE --qty 1 --confirm

The order is CASH/MARKET/BUY only, capped at 1 share and a hardcoded max
order value, so a mistake costs at most a few hundred rupees. Since
PRODUCT_MIS requires a same-day exit, you are responsible for squaring off
the resulting position yourself (via the Groww app) before market close —
this script does not manage or track the position afterward.
"""
import argparse
import os
import sys

from dotenv import load_dotenv

from core.auth import get_client, GrowwAuthError
from core.execution import LiveBroker
from core.models import ProposedOrder, Side, OrderType, Segment

MAX_QTY = 1
MAX_ORDER_VALUE_INR = 1000
CONFIRMATION_PHRASE = "PLACE REAL ORDER"


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="RELIANCE")
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--confirm", action="store_true",
                         help="Required flag acknowledging this places a real order.")
    args = parser.parse_args()

    if args.qty > MAX_QTY:
        print(f"Refusing: qty {args.qty} exceeds this script's hardcoded cap of {MAX_QTY}.")
        sys.exit(1)

    if os.getenv("MODE") != "LIVE":
        print("Refusing: MODE=LIVE is not set in .env. This script only runs in live mode.")
        sys.exit(1)

    if not args.confirm:
        print("Refusing: pass --confirm to acknowledge this places a real order with real money.")
        sys.exit(1)

    try:
        client = get_client()
    except GrowwAuthError as e:
        print(f"Groww auth failed: {e}")
        sys.exit(1)

    broker = LiveBroker(client)

    ltp = broker.get_ltp(args.symbol, segment=Segment.CASH)
    if ltp is None:
        print(f"Could not fetch LTP for {args.symbol}. Aborting.")
        sys.exit(1)

    est_value = ltp * args.qty
    print(f"Symbol: {args.symbol}  LTP: {ltp}  Qty: {args.qty}  Est. value: INR {est_value:.2f}")

    if est_value > MAX_ORDER_VALUE_INR:
        print(f"Refusing: estimated order value INR {est_value:.2f} exceeds "
              f"this script's hardcoded cap of INR {MAX_ORDER_VALUE_INR}.")
        sys.exit(1)

    print(f"\nThis will place a REAL MARKET BUY order for {args.qty} share(s) of "
          f"{args.symbol} (~INR {est_value:.2f}), product=MIS — you must square it "
          f"off yourself today via the Groww app.")
    typed = input(f'Type "{CONFIRMATION_PHRASE}" to proceed: ')
    if typed != CONFIRMATION_PHRASE:
        print("Confirmation phrase did not match. Aborting.")
        sys.exit(1)

    order = ProposedOrder(
        symbol=args.symbol,
        side=Side.BUY,
        qty=args.qty,
        order_type=OrderType.MARKET,
        segment=Segment.CASH,
        reason="Manual LiveBroker smoke test",
    )

    result = broker.place_order(order, last_traded_price=ltp)
    print(f"\nStatus: {result.status}")
    print(f"Broker order id: {result.broker_order_id}")
    print(f"Message: {result.message}")

    if result.status not in ("FILLED", "ERROR", "REJECTED"):
        print("\nOrder is pending/in-flight — check the Groww app for the final fill status.")
    print("\nReminder: this position is NOT tracked by the bot's DB or dashboard. "
          "Square it off manually before market close.")


if __name__ == "__main__":
    main()
