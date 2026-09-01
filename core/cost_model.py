"""
Transaction cost model for NSE CASH-segment INTRADAY (MIS) orders, matching Groww's
published fee schedule (groww.in/pricing, groww.in/help "What are intraday charges?" —
checked 2026-08-28). A documented approximation for P&L realism, not a literal
contract-note replica — Groww's minimum-brokerage sub-clause ("₹5 or 2.5% of trade
value, whichever lower") and the tiny Investor Protection Fund Trust charge (0.0001%,
both sides) are folded into / omitted from this model. Rates change; revisit this file
if Groww's published schedule moves.

Applies uniformly wherever PaperBroker/LiveBroker fill a CASH order (live, paper, and
backtest all share core/orchestrator.py's _handle_proposed_order()) — a paper trade
should cost what a real one would, or PAPER-mode P&L numbers mean nothing once real
trading begins. FNO has a materially different fee structure (different STT, no
buy-side stamp duty the same way) and is deliberately not modeled here — CASH only.
"""
from core.models import Side

BROKERAGE_RATE = 0.001          # 0.1% of trade value
BROKERAGE_CAP_INR = 20.0        # per executed order, whichever is lower
STT_SELL_RATE = 0.00025         # 0.025%, intraday equity — SELL side only
EXCHANGE_TXN_RATE = 0.0000297   # NSE transaction charge, both sides
SEBI_TURNOVER_RATE = 0.000001   # 0.0001%, both sides
STAMP_DUTY_BUY_RATE = 0.00003   # 0.003%, BUY side only
# GST applies to broker/exchange/regulatory service charges, not to STT or stamp duty
# (those are themselves taxes).
GST_RATE = 0.18


def calculate_order_charges(trade_value: float, side: Side) -> float:
    """Total transaction cost for ONE executed CASH-segment intraday order leg. A full
    BUY+SELL round trip incurs this twice — once per leg — with STT and stamp duty each
    applying to only their one respective side, matching how NSE/Groww actually charge
    them (never doubled up on both legs of a round trip)."""
    brokerage = min(trade_value * BROKERAGE_RATE, BROKERAGE_CAP_INR)
    stt = trade_value * STT_SELL_RATE if side == Side.SELL else 0.0
    exchange_txn = trade_value * EXCHANGE_TXN_RATE
    sebi_turnover = trade_value * SEBI_TURNOVER_RATE
    stamp_duty = trade_value * STAMP_DUTY_BUY_RATE if side == Side.BUY else 0.0
    gst = (brokerage + exchange_txn + sebi_turnover) * GST_RATE
    return round(brokerage + stt + exchange_txn + sebi_turnover + stamp_duty + gst, 4)


AUTO_SQUARE_OFF_PENALTY_INR = 50.0  # Groww's flat per-position MIS-not-squared-off fee


def calculate_square_off_penalty() -> float:
    """Flat fee for a CASH MIS position force-closed by the broker at the 3:20 PM IST
    cutoff, plus GST. A separate line item from calculate_order_charges() (independent of
    trade value) — added on top of it only for a forced square-off's exit charges."""
    return round(AUTO_SQUARE_OFF_PENALTY_INR * (1 + GST_RATE), 2)
