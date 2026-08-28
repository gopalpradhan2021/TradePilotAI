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

    # For FNO orders, `symbol` is the exact broker trading_symbol for the specific
    # contract (e.g. "NIFTY2690122000CE") — see core/execution.py::_build_trading_symbol.
    # `underlying_symbol` carries the underlying separately (e.g. "NIFTY") since the two
    # are no longer the same value once `symbol` identifies a specific contract. None for
    # CASH orders, where `symbol` already is the underlying.
    underlying_symbol: str | None = None

    @property
    def total_units(self) -> int:
        return self.qty * (self.lot_size if self.segment == Segment.FNO else 1)


@dataclass
class OptionGreeks:
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    iv: float


@dataclass
class OptionQuote:
    trading_symbol: str
    ltp: float | None
    open_interest: int
    volume: int
    greeks: OptionGreeks


@dataclass
class StrikeQuote:
    strike: float
    ce: OptionQuote | None
    pe: OptionQuote | None


@dataclass
class OptionChainSnapshot:
    """Pure market data — no broker/account reference — so strategies consuming this via
    BaseStrategy.decide_fno() stay as broker-agnostic as decide() already is with an LTP
    float. One snapshot covers one underlying's one expiry."""
    underlying: str
    underlying_ltp: float
    expiry_date: date
    fetched_at: datetime
    strikes: list[StrikeQuote]

    def find_quote(self, trading_symbol: str) -> "OptionQuote | None":
        """Looks up a specific contract's live quote by its trading_symbol — e.g. to get
        an option's own premium (NOT underlying_ltp, which is the underlying's spot price
        and is NOT the traded instrument's own price) when funneling a ProposedOrder
        through risk_manager.check()/broker.place_order()."""
        for s in self.strikes:
            if s.ce is not None and s.ce.trading_symbol == trading_symbol:
                return s.ce
            if s.pe is not None and s.pe.trading_symbol == trading_symbol:
                return s.pe
        return None


@dataclass
class MarginQuote:
    required_margin: float
    available_margin: float


@dataclass
class RiskCheckResult:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    # Populated only for FNO orders checked with a margin_provider configured — lets the
    # orchestrator record the real margin at fill time without a second API call.
    margin_quote: MarginQuote | None = None


@dataclass
class ExecutionResult:
    order: ProposedOrder
    status: str
    broker_order_id: str | None = None
    fill_price: float | None = None
    message: str = ""
