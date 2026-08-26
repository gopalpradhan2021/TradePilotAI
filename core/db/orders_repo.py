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
                    limit_price, expiry_date, strike_price, option_type, reason,
                    status, reference_price, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.idempotency_key, order.symbol, order.side.value, order.order_type.value,
                    order.segment.value, order.qty, order.lot_size, order.limit_price,
                    order.expiry_date.isoformat() if order.expiry_date else None,
                    order.strike_price,
                    order.option_type.value if order.option_type else None,
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
                         fill_price: float | None = None, message: str = "") -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE orders
            SET status = ?, broker_order_id = COALESCE(?, broker_order_id),
                fill_price = COALESCE(?, fill_price), message = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, broker_order_id, fill_price, message, _now(), order_id),
        )
        conn.commit()


def get_recent_orders(limit: int | None = 20) -> list[dict]:
    query = """
        SELECT symbol, side, qty, segment, status, fill_price, reason, message, created_at
        FROM orders ORDER BY created_at DESC, id DESC
    """
    params: tuple = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
