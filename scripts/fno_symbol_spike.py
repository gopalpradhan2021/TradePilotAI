"""
Standalone, read-only research script: probes candidate futures trading_symbol formats
against the real Groww API to find one it actually accepts for order placement.

_build_trading_symbol()'s options branch was fixed by using get_option_chain()'s own
trading_symbol field directly (a hand-constructed guess turned out wrong — see
core/execution.py). No equivalent "give me the ready order-placement symbol" endpoint
was found live for futures; get_contracts() returns a differently-formatted contract
identifier ("NSE-NIFTY-29Sep26-24900-CE") that get_quote()/place_order() do not accept.

This script tries several plausible formats against get_quote() (read-only — a successful
quote fetch means the format at least resolves to a real instrument; it does NOT place an
order) and reports which ones the backend recognizes. Run manually, not part of CI/pytest.

Usage:
    python -m scripts.fno_symbol_spike --underlying NIFTY --expiry 2026-09-29
"""
import argparse
import sys
from datetime import date


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--expiry", required=True, help="YYYY-MM-DD")
    return parser.parse_args()


def main():
    args = _parse_args()
    expiry = date.fromisoformat(args.expiry)

    from core.auth import get_client, GrowwAuthError

    try:
        client = get_client()
    except GrowwAuthError as e:
        print(f"Groww auth failed: {e}")
        sys.exit(1)

    candidates = {
        "YY+3LETTERMONTH+FUT (current guess in _build_trading_symbol)":
            f"{args.underlying}{expiry.strftime('%y%b').upper()}FUT",
        "YY+M(single digit)+DD+FUT (matches get_option_chain's option format)":
            f"{args.underlying}{expiry.strftime('%y')}{expiry.month}{expiry.strftime('%d')}FUT",
        "YYMMDD+FUT":
            f"{args.underlying}{expiry.strftime('%y%m%d')}FUT",
        "DDMONYY dash-separated, no exchange prefix (get_contracts format minus 'NSE-')":
            f"{args.underlying}-{expiry.strftime('%d%b%y')}-FUT",
    }

    print(f"Probing futures trading_symbol candidates for {args.underlying} "
          f"expiry {expiry.isoformat()}:\n")
    working = []
    for label, symbol in candidates.items():
        try:
            quote = client.get_quote(
                exchange=client.EXCHANGE_NSE, segment=client.SEGMENT_FNO,
                trading_symbol=symbol,
            )
            ltp = quote.get("last_price") or quote.get("ltp")
            print(f"  WORKS  {symbol!r:40s} ({label}) — ltp={ltp}")
            working.append(symbol)
        except Exception as e:
            print(f"  fails  {symbol!r:40s} ({label}) — {e}")

    print()
    if working:
        print(f"Found {len(working)} working format(s): {working}")
        print("Update _build_trading_symbol()'s futures branch in core/execution.py "
              "to match before Phase D ships.")
    else:
        print("None of the candidate formats resolved. Try get_all_instruments() or "
              "get_instrument_by_exchange_and_trading_symbol() to find the real one, or "
              "add more candidates to this script — do not guess-and-ship for futures.")


if __name__ == "__main__":
    main()
