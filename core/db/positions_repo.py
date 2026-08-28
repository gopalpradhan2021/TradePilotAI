from datetime import datetime, timezone

from core.db.connection import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_open_position(symbol: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE symbol = ? AND status = 'OPEN'", (symbol,)
        ).fetchone()
        return dict(row) if row else None


def open_position(symbol: str, qty: int, entry_price: float, entry_order_id: int,
                   segment: str = "CASH", underlying_symbol: str | None = None,
                   margin_used: float | None = None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO positions
                (symbol, status, qty, entry_price, entry_order_id, opened_at,
                 segment, underlying_symbol, margin_used)
            VALUES (?, 'OPEN', ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, qty, entry_price, entry_order_id, _now(),
             segment, underlying_symbol, margin_used),
        )
        conn.commit()
        return cur.lastrowid


def close_position(symbol: str, exit_price: float, exit_order_id: int) -> float:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, qty, entry_price FROM positions WHERE symbol = ? AND status = 'OPEN'",
            (symbol,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No open position for {symbol} to close.")

        realized_pnl = (exit_price - row["entry_price"]) * row["qty"]
        conn.execute(
            """
            UPDATE positions
            SET status = 'CLOSED', exit_price = ?, exit_order_id = ?, realized_pnl = ?, closed_at = ?
            WHERE id = ?
            """,
            (exit_price, exit_order_id, realized_pnl, _now(), row["id"]),
        )
        conn.commit()
        return realized_pnl


def get_deployed_capital() -> float:
    # FNO capital-at-risk is real margin, not notional (qty*entry_price) — margin_used is
    # captured at fill time from RiskManager's margin check, see core/orchestrator.py.
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE WHEN segment = 'FNO' THEN COALESCE(margin_used, 0)
                     ELSE qty * entry_price END
            ), 0) AS total
            FROM positions WHERE status = 'OPEN'
            """
        ).fetchone()
        return row["total"]


def get_open_positions() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT symbol, qty, entry_price, opened_at, segment, underlying_symbol, margin_used
            FROM positions WHERE status = 'OPEN'
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_closed_positions() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT symbol, qty, entry_price, exit_price, realized_pnl, opened_at, closed_at
            FROM positions WHERE status = 'CLOSED' ORDER BY closed_at ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_win_loss_counts() -> tuple[int, int]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) AS losses
            FROM positions WHERE status = 'CLOSED'
            """
        ).fetchone()
        return (row["wins"] or 0, row["losses"] or 0)
