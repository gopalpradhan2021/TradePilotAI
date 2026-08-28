"""
Compares local position records against what the broker actually reports. Used both at
startup (main.py, before entering the trading loop) and periodically during the trading loop
itself (core/orchestrator.py) — same check, same halt behavior, just a different cadence.
"""
from core.db import positions_repo
from core.execution import BrokerPositionFetchError
from core.models import Segment


def _check_one(broker, risk_manager, trading_symbol: str, local_qty: int,
                segment: Segment, logger) -> None:
    try:
        broker_qty = broker.get_broker_position(trading_symbol, segment=segment)["qty"]
    except BrokerPositionFetchError as e:
        reason = f"Reconciliation: could not fetch broker position for {trading_symbol}: {e}"
        logger.error(reason)
        risk_manager.halt_reconciliation_mismatch(reason)
        return
    if local_qty != broker_qty:
        reason = (
            f"Position mismatch for {trading_symbol}: local DB shows {local_qty}, "
            f"broker reports {broker_qty}."
        )
        logger.critical("RECONCILIATION MISMATCH: %s", reason)
        risk_manager.halt_reconciliation_mismatch(reason)


def reconcile_positions(broker, risk_manager, symbols: list[str], logger) -> None:
    """Halts (AUTO — can't be casually resumed) on any mismatch or fetch failure, since a
    divergence here means the local ledger can't be trusted.

    CASH: one lookup per watched `symbols` entry (unchanged) — for CASH, symbol IS the
    traded instrument, and checking the full watched universe (not just symbols with an
    existing local row) also catches "broker has a position we don't know about at all"
    (e.g. a manual trade placed directly in the Groww app).

    FNO: positions.symbol is the specific contract's trading_symbol (e.g.
    "NIFTY2690122000CE"), not the underlying, and one underlying can back several
    simultaneous positions (multiple strikes/expiries) — `symbols`/`fno_underlyings`
    can't enumerate that contract space the way CASH's watched-symbol list can. Instead,
    every currently-OPEN local FNO position row is checked against the broker. This can
    only catch "local shows a position the broker disagrees with" for contracts we already
    have a record of — not "broker has an FNO position we've never heard of at all",
    which is infeasible to check without enumerating every possible contract."""
    for symbol in symbols:
        local_pos = positions_repo.get_open_position(symbol)
        local_qty = local_pos["qty"] if local_pos else 0
        _check_one(broker, risk_manager, symbol, local_qty, Segment.CASH, logger)

    for pos in positions_repo.get_open_positions():
        if pos.get("segment") != "FNO":
            continue  # CASH already covered by the loop above
        _check_one(broker, risk_manager, pos["symbol"], pos["qty"], Segment.FNO, logger)
