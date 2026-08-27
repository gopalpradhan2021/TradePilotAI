"""
Strategy interface. Implement `decide()` and return either a ProposedOrder
or None (no action this cycle).
"""
from abc import ABC, abstractmethod
from core.models import ProposedOrder


class BaseStrategy(ABC):
    @abstractmethod
    def decide(self, symbol: str, last_traded_price: float | None) -> ProposedOrder | None:
        ...

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
