"""
Capital-aware position sizing for CASH orders. A pure function, no I/O — the caller
(core/orchestrator.py) supplies the account-state inputs, since strategies themselves
never see capital/account state (CLAUDE.md: "strategies... only see price, never
account/position state").

Reuses the existing RiskConfig caps (max_order_value_inr, total_capital_inr minus
deployed capital, max_position_qty) rather than introducing a new tunable — sizes up to
fill whichever cap binds first.
"""


def calculate_entry_qty(entry_price: float, available_capital: float,
                         max_order_value_inr: float, max_position_qty: int) -> int:
    if entry_price <= 0:
        return 0
    qty_by_value_cap = int(max_order_value_inr // entry_price)
    qty_by_capital = int(available_capital // entry_price)
    return max(min(qty_by_value_cap, qty_by_capital, max_position_qty), 0)
