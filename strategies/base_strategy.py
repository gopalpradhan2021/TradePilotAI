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


class NoOpStrategy(BaseStrategy):
    def decide(self, symbol: str, last_traded_price: float | None) -> ProposedOrder | None:
        return None
