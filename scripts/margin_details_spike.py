"""
Standalone, read-only research script: confirms the exact request/response shape of
Groww's get_order_margin_details() / get_available_margin_details() against the real API.

Already resolved once (2026-08-28) by reading growwapi's own source
(inspect.getsource(GrowwAPI.get_order_margin_details)) and one live call — required keys
per order dict are: trading_symbol, transaction_type, quantity, price, order_type, product,
exchange. Response carries required margin in "total_requirement". Available margin per
side lives in get_available_margin_details()["fno_margin_details"]
(future_balance_available / option_buy_balance_available / option_sell_balance_available).
See core/margin_provider.py::GrowwMarginProvider, which is written against this shape.

Re-run this script if GrowwMarginProvider ever starts getting KeyErrors or unexpected
values back — Groww's response shape could change without notice, same as the trading
symbol format did for options. Run manually, not part of CI/pytest.

Usage:
    python -m scripts.margin_details_spike --underlying NIFTY --expiry 2026-09-29
"""
import argparse
import json
import sys
from datetime import date


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--expiry", required=True, help="YYYY-MM-DD, must have a live option chain")
    return parser.parse_args()


def main():
    args = _parse_args()

    from core.auth import get_client, GrowwAuthError

    try:
        client = get_client()
    except GrowwAuthError as e:
        print(f"Groww auth failed: {e}")
        sys.exit(1)

    chain = client.get_option_chain(
        exchange=client.EXCHANGE_NSE, underlying=args.underlying, expiry_date=args.expiry,
    )
    strikes = chain.get("strikes", {})
    if not strikes:
        print(f"No strikes returned for {args.underlying} {args.expiry} — pick a real expiry "
              "(see get_expiries()).")
        sys.exit(1)

    mid_strike = sorted(strikes.keys(), key=float)[len(strikes) // 2]
    trading_symbol = strikes[mid_strike]["CE"]["trading_symbol"]
    print(f"Using {trading_symbol!r} (strike {mid_strike} CE) for the margin probe.\n")

    print("--- get_order_margin_details() ---")
    try:
        margin = client.get_order_margin_details(
            segment=client.SEGMENT_FNO,
            orders=[{
                "trading_symbol": trading_symbol,
                "transaction_type": client.TRANSACTION_TYPE_BUY,
                "quantity": 75,
                "price": 0,
                "order_type": client.ORDER_TYPE_MARKET,
                "product": client.PRODUCT_NRML,
                "exchange": client.EXCHANGE_NSE,
            }],
        )
        print(json.dumps(margin, indent=2, default=str))
        if "total_requirement" not in margin:
            print("\nWARNING: 'total_requirement' key missing — GrowwMarginProvider "
                  "assumes this key exists. Update it if the response shape changed.")
    except Exception as e:
        print(f"get_order_margin_details() failed: {e!r}")

    print("\n--- get_available_margin_details() ---")
    try:
        available = client.get_available_margin_details()
        print(json.dumps(available, indent=2, default=str))
        fno = available.get("fno_margin_details", {})
        for key in ("future_balance_available", "option_buy_balance_available",
                    "option_sell_balance_available"):
            if key not in fno:
                print(f"\nWARNING: fno_margin_details.{key} missing — "
                      "GrowwMarginProvider assumes this key exists.")
    except Exception as e:
        print(f"get_available_margin_details() failed: {e!r}")


if __name__ == "__main__":
    main()
