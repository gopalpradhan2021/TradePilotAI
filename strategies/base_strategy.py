"""
Strategy interface. A strategy implements decide() (CASH), decide_fno() (F&O), or both,
returning a ProposedOrder or None (no action this cycle) either way.

Neither method is abstract: a CASH-only strategy (MARsiStrategy) needs only decide();
an F&O-only strategy (IvOiStrategy) needs only decide_fno(). Both default to a no-op so
a strategy implementing just one isn't forced to stub out the other.
"""
from abc import ABC
from core.models import OptionChainSnapshot, ProposedOrder


class BaseStrategy(ABC):
    def decide(self, symbol: str, last_traded_price: float | None) -> ProposedOrder | None:
        return None

    def decide_fno(self, underlying: str, chain: OptionChainSnapshot) -> ProposedOrder | None:
        """F&O counterpart to decide() — takes a full option-chain snapshot (IV, OI,
        Greeks per strike) instead of a single LTP float, since that's the minimum an
        options strategy actually needs. Pure market data, no broker/account reference —
        keeps this exactly as broker-agnostic as decide() already is."""
        return None

    def get_candle_requirements(self) -> tuple[str, int] | None:
        """Optional: (candle_interval, lookback_bars) this strategy needs Orchestrator to
        fetch via broker.get_recent_candles() and hand to update_candles(). Default: None —
        this strategy computes off decide()'s last_traded_price only."""
        return None

    def update_candles(self, symbol: str, candles: list[dict]) -> None:
        """Optional: REPLACES (not appends) this symbol's candle series — called by
        Orchestrator on the candle-fetch cadence, not every decide() cycle. Default: no-op."""
        pass

    def restore_position(self, symbol: str, entry_price: float) -> None:
        """Optional: called once per symbol at startup if positions_repo shows an
        already-open position, so the strategy resumes tracking it instead of starting
        cold after a restart. Default: no-op."""
        pass

    def get_debug_info(self, symbol: str) -> dict:
        """Optional: a snapshot of whatever internal state is useful to show an operator
        (indicator values, how close to a signal, warmup progress) — purely observational,
        never used by decide() itself. Orchestrator forwards this into the heartbeat file so
        the dashboard can show it without any direct connection to the bot process. Default:
        empty (nothing to show)."""
        return {}


class NoOpStrategy(BaseStrategy):
    def decide(self, symbol: str, last_traded_price: float | None) -> ProposedOrder | None:
        return None
