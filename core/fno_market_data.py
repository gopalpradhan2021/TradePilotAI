"""
Thin adapter between the orchestrator and Broker.get_expiries()/get_option_chain() —
picks which expiry to trade (nearest upcoming) so strategies/orchestrator don't each
need their own expiry-selection logic.

Raw-response parsing itself lives in core/execution.py (Broker.get_option_chain already
returns a fully-parsed OptionChainSnapshot, not a raw dict) — this module is business
logic (which expiry), not a parser.
"""
import logging
from datetime import date
from typing import Callable

from core.models import OptionChainSnapshot

logger = logging.getLogger("groww_agent.fno_market_data")


class FnoMarketData:
    def __init__(self, broker, today_fn: Callable[[], date] = date.today):
        self._broker = broker
        self._today_fn = today_fn

    def get_chain(self, underlying: str) -> OptionChainSnapshot | None:
        """Fetches the nearest upcoming expiry's option chain for `underlying`. Returns
        None if expiries or the chain can't be fetched — Broker's own methods already
        return None on any fetch failure; this only adds the expiry-selection step, not
        new failure handling."""
        expiries = self._broker.get_expiries(underlying)
        if not expiries:
            logger.warning("No expiries returned for %s — skipping this cycle.", underlying)
            return None

        today = self._today_fn()
        upcoming = [e for e in expiries if e >= today]
        if not upcoming:
            logger.warning("No upcoming expiries for %s among %d returned — skipping.",
                            underlying, len(expiries))
            return None

        nearest = min(upcoming)
        return self._broker.get_option_chain(underlying, nearest)
