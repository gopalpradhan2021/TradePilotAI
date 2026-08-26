import json
from datetime import datetime, timezone

from core.db.connection import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_or_create_daily_summary(trade_date: str) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM daily_summary WHERE trade_date = ?", (trade_date,)
        ).fetchone()
        if row is not None:
            return dict(row)

        now = _now()
        conn.execute(
            """
            INSERT INTO daily_summary
                (trade_date, trades_count, realized_pnl, halted, halt_reason, halt_source, created_at, updated_at)
            VALUES (?, 0, 0, 0, '', 'AUTO', ?, ?)
            """,
            (trade_date, now, now),
        )
        conn.commit()
        return {
            "trade_date": trade_date, "trades_count": 0, "realized_pnl": 0.0,
            "halted": 0, "halt_reason": "", "halt_source": "AUTO", "created_at": now, "updated_at": now,
        }


def increment_daily_counters(trade_date: str, pnl_delta: float) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE daily_summary
            SET trades_count = trades_count + 1, realized_pnl = realized_pnl + ?, updated_at = ?
            WHERE trade_date = ?
            """,
            (pnl_delta, _now(), trade_date),
        )
        conn.commit()


def set_halted(trade_date: str, halted: bool, reason: str, source: str = "AUTO") -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE daily_summary SET halted = ?, halt_reason = ?, halt_source = ?, updated_at = ?
            WHERE trade_date = ?
            """,
            (1 if halted else 0, reason, source, _now(), trade_date),
        )
        conn.commit()


def get_halt_state(trade_date: str) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT halted, halt_reason, halt_source FROM daily_summary WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
        if row is None:
            return {"halted": False, "halt_reason": "", "halt_source": "AUTO"}
        return dict(row)


def record_risk_event(*, order_id: int | None, symbol: str | None, event_type: str,
                       reasons: list[str], reference_price: float | None = None,
                       order_value: float | None = None, deployed_capital: float | None = None,
                       trades_today: int | None = None, realized_pnl_today: float | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO risk_events (
                order_id, symbol, event_type, reasons, reference_price, order_value,
                deployed_capital_at_event, trades_today_at_event, realized_pnl_today_at_event, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (order_id, symbol, event_type, json.dumps(reasons), reference_price, order_value,
             deployed_capital, trades_today, realized_pnl_today, _now()),
        )
        conn.commit()
