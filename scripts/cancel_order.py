"""
Operator emergency tool: cancel a single resting order directly against Groww.

Standalone and independent of the running bot process (same pattern as scripts/halt_bot.py) —
requires MODE=LIVE in .env and a typed confirmation, since this places a real cancel request
against a real broker order.

Usage:
    python -m scripts.cancel_order --order-id <groww_order_id> [--segment CASH]
"""
import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("groww_agent.cancel_order")

CONFIRMATION_PHRASE = "CANCEL ORDER"


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-id", required=True, help="Groww order id to cancel.")
    parser.add_argument("--segment", default="CASH", choices=["CASH", "FNO"])
    return parser.parse_args()


def main():
    args = _parse_args()

    if os.getenv("MODE", "PAPER").upper() != "LIVE":
        print("Refusing: MODE=LIVE is not set in .env. This tool only operates against real orders.")
        sys.exit(1)

    from core.auth import get_client, GrowwAuthError
    from core.execution import LiveBroker, BrokerPositionFetchError
    from core.models import Segment

    print(f"About to cancel order {args.order_id} (segment={args.segment}) — this is a REAL "
          f"cancellation against a real broker order.")
    typed = input(f'Type "{CONFIRMATION_PHRASE}" to proceed: ')
    if typed != CONFIRMATION_PHRASE:
        print("Confirmation phrase did not match. Aborting.")
        sys.exit(1)

    try:
        client = get_client()
    except GrowwAuthError as e:
        print(f"Groww auth failed: {e}")
        sys.exit(1)

    broker = LiveBroker(client)
    try:
        response = broker.cancel_order(args.order_id, segment=Segment[args.segment])
    except BrokerPositionFetchError as e:
        print(f"Cancel FAILED: {e}")
        print("Do not assume the order is still resting — check the Groww app directly.")
        sys.exit(1)

    print(f"Cancel response: {response}")
    print("Verify the final state in the Groww app or dashboard — this script does not poll "
          "for confirmation.")


if __name__ == "__main__":
    main()
