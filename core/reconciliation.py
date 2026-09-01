"""
Compares local position records against what the broker actually reports. Used both at
startup (main.py, before entering the trading loop) and periodically during the trading loop
itself (core/orchestrator.py) — same check, same halt behavior, just a different cadence.
"""
import sqlite3

from core.db import orders_repo, positions_repo
from core.execution import BrokerPositionFetchError
from core.models import Segment, Side


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


def reconcile_orphaned_fills(risk_manager, logger) -> None:
    """Startup-only, broker-independent integrity check (runs in BOTH modes — this is a
    purely local orders-vs-positions consistency check, not a broker comparison): finds a
    FILLED BUY order with no positions row and reopens the position deterministically from
    the order's own recorded fill_price/qty/segment/margin/charges.

    This closes the gap left by Orchestrator._handle_proposed_order() doing
    update_order_status(FILLED) and positions_repo.open_position() as two separate
    connections/transactions (see core/db/connection.py) — a process kill landing between
    them leaves a FILLED order with no position, silently losing track of a real fill. Since
    the order row already carries everything needed to reconstruct the position exactly (not
    a guess, unlike a broker-side mismatch), recovery is safe to do automatically. Also
    replays the risk_manager.record_fill() bookkeeping (trade count) that never ran because
    the crash pre-empted it too.

    If a *different* order already holds the OPEN slot for that symbol, the position can't
    be reopened (the DB's one-open-position-per-symbol unique index rejects it) — that's an
    unresolvable local inconsistency, so this halts (AUTO) instead of guessing, same as a
    broker mismatch.
    """
    for order in orders_repo.get_filled_buy_orders_without_position():
        symbol = order["symbol"]
        logger.critical(
            "RECONCILIATION: orphaned FILLED order id=%s %s qty=%s @ %s had no positions "
            "row (likely a process restart mid-fill) — reopening position from the order's "
            "own record.", order["id"], symbol, order["qty"], order["fill_price"],
        )
        try:
            positions_repo.open_position(
                symbol=symbol, qty=order["qty"], entry_price=order["fill_price"],
                entry_order_id=order["id"], segment=order.get("segment", "CASH"),
                underlying_symbol=order.get("underlying_symbol"),
                margin_used=order.get("margin_used"), entry_charges=order.get("charges") or 0.0,
            )
        except sqlite3.IntegrityError as e:
            reason = (
                f"Reconciliation: orphaned FILLED order id={order['id']} for {symbol} could "
                f"not be recovered — another OPEN position already exists for this symbol "
                f"({e})."
            )
            logger.critical(reason)
            risk_manager.halt_reconciliation_mismatch(reason)
            continue

        order_value = order["fill_price"] * order["qty"]
        risk_manager.record_fill(side=Side.BUY, order_value=order_value, pnl_delta=0.0,
                                  order_id=order["id"])
