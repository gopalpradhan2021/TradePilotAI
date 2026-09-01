import sqlite3
from datetime import datetime, timezone

from core.db.connection import get_connection
from core.models import ProposedOrder


class DuplicateIdempotencyKeyError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_order(order: ProposedOrder, status: str, reference_price: float | None) -> int:
    now = _now()
    with get_connection() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO orders (
                    idempotency_key, symbol, side, order_type, segment, qty, lot_size,
                    limit_price, expiry_date, strike_price, option_type, underlying_symbol,
                    reason, status, reference_price, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.idempotency_key, order.symbol, order.side.value, order.order_type.value,
                    order.segment.value, order.qty, order.lot_size, order.limit_price,
                    order.expiry_date.isoformat() if order.expiry_date else None,
                    order.strike_price,
                    order.option_type.value if order.option_type else None,
                    order.underlying_symbol,
                    order.reason, status, reference_price, now, now,
                ),
            )
        except sqlite3.IntegrityError as e:
            raise DuplicateIdempotencyKeyError(
                f"idempotency_key {order.idempotency_key} already exists"
            ) from e
        conn.commit()
        return cur.lastrowid


def update_order_status(order_id: int, *, status: str, broker_order_id: str | None = None,
                         fill_price: float | None = None, message: str = "",
                         margin_used: float | None = None, charges: float | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE orders
            SET status = ?, broker_order_id = COALESCE(?, broker_order_id),
                fill_price = COALESCE(?, fill_price), margin_used = COALESCE(?, margin_used),
                charges = COALESCE(?, charges),
                message = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, broker_order_id, fill_price, margin_used, charges, message, _now(), order_id),
        )
        conn.commit()


def get_filled_buy_orders_without_position() -> list[dict]:
    """FILLED BUY orders with no positions row referencing them as entry_order_id — the
    signature of a process kill landing between Orchestrator._handle_proposed_order()'s
    update_order_status(FILLED) and positions_repo.open_position() calls (each opens its
    own short-lived connection, see core/db/connection.py — the two writes aren't atomic).
    fill_price IS NOT NULL excludes the unrelated, intentional case of a FILLED result with
    no fill_price, which core/orchestrator.py already skips opening a position for.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT o.* FROM orders o
            LEFT JOIN positions p ON p.entry_order_id = o.id
            WHERE o.side = 'BUY' AND o.status = 'FILLED' AND o.fill_price IS NOT NULL
                  AND p.id IS NULL
            ORDER BY o.created_at ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_orders(limit: int | None = 20) -> list[dict]:
    # LEFT JOIN positions on exit_order_id: realized_pnl only exists for the SELL that
    # actually closed a position (positions.exit_order_id -> orders.id) — NULL for every
    # BUY and for any order that never closed anything, which the dashboard renders as "—".
    query = """
        SELECT o.symbol, o.side, o.qty, o.segment, o.underlying_symbol, o.status,
               o.fill_price, o.charges, o.reason, o.message, o.created_at,
               p.realized_pnl AS realized_pnl
        FROM orders o
        LEFT JOIN positions p ON p.exit_order_id = o.id
        ORDER BY o.created_at DESC, o.id DESC
    """
    params: tuple = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
