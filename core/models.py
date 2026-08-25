from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
import uuid


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class Segment(str, Enum):
    CASH = "CASH"
    FNO = "FNO"


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


@dataclass
class ProposedOrder:
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    segment: Segment = Segment.CASH
    limit_price: float | None = None
    reason: str = ""
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.utcnow)

    lot_size: int = 1
    expiry_date: date | None = None
    strike_price: float | None = None
    option_type: OptionType | None = None

    @property
    def total_units(self) -> int:
        return self.qty * (self.lot_size if self.segment == Segment.FNO else 1)


@dataclass
class RiskCheckResult:
    approved: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    order: ProposedOrder
    status: str
    broker_order_id: str | None = None
    fill_price: float | None = None
    message: str = ""
