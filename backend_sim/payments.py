"""Simulated payment provider. issue_refund is idempotent on idempotency_key,
enforced by the UNIQUE constraint rather than by a read-then-write check."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from backend_sim.db import Db, get_db


class PaymentError(Exception):
    pass


def issue_refund(order_id: str, amount_cents: int, idempotency_key: str,
                 actor: str = "refund_desk", db: Db | None = None) -> tuple[str, bool]:
    """Returns (refund_id, created). A replay returns the original refund, created=False."""
    db = db or get_db()
    order = db.one("SELECT total_cents FROM orders WHERE id = %s", (order_id,))
    if order is None:
        raise PaymentError(f"unknown order {order_id}")
    if amount_cents < 0 or amount_cents > order["total_cents"]:
        raise PaymentError("amount outside 0..order total")
    new_id = f"re_{uuid.uuid4().hex[:12]}"
    n = db.execute(
        "INSERT INTO refunds (id, order_id, amount_cents, idempotency_key, created_at, actor)"
        " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (idempotency_key) DO NOTHING",
        (new_id, order_id, amount_cents, idempotency_key, db.ts(datetime.now(timezone.utc)), actor))
    row = db.one("SELECT id, order_id, amount_cents FROM refunds WHERE idempotency_key = %s", (idempotency_key,))
    if row["order_id"] != order_id or row["amount_cents"] != amount_cents:
        raise PaymentError("idempotency key reused with different parameters")
    return row["id"], n == 1


def get_refund(idempotency_key: str, db: Db | None = None) -> dict | None:
    return (db or get_db()).one("SELECT * FROM refunds WHERE idempotency_key = %s", (idempotency_key,))
