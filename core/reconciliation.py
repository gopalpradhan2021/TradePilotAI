"""
Compares local position records against what the broker actually reports. Used both at
startup (main.py, before entering the trading loop) and periodically during the trading loop
itself (core/orchestrator.py) — same check, same halt behavior, just a different cadence.
"""
from core.db import positions_repo
from core.execution import BrokerPositionFetchError


def reconcile_positions(broker, risk_manager, symbols: list[str], logger) -> None:
    """Halts (AUTO — can't be casually resumed) on any mismatch or fetch failure, since a
    divergence here means the local ledger can't be trusted."""
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
                f"Position mismatch for {symbol}: local DB shows {local_qty}, "
                f"broker reports {broker_qty}."
            )
            logger.critical("RECONCILIATION MISMATCH: %s", reason)
            risk_manager.halt_reconciliation_mismatch(reason)
