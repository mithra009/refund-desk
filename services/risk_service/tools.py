"""Risk signal tools for the ADK agents. They read the shared fake backend, read-only."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from backend_sim.db import get_db, parse_ts
from backend_sim.factsheet import AS_OF


def _now() -> datetime:
    raw = os.environ.get("RISK_AS_OF")
    return datetime.fromisoformat(raw) if raw else AS_OF


def refund_velocity(customer_id: str) -> dict:
    """Number of refunds issued to this customer in the last 90 days.

    Args:
        customer_id: the customer id from the request.
    """
    rows = get_db().all(
        "SELECT r.created_at FROM refunds r JOIN orders o ON o.id = r.order_id WHERE o.customer_id = %s",
        (customer_id,))
    since = _now() - timedelta(days=90)
    return {"customer_id": customer_id, "refunds_last_90d": sum(1 for r in rows if parse_ts(r["created_at"]) >= since)}


def address_reuse(customer_id: str) -> dict:
    """How many other accounts share this customer's delivery address.

    Args:
        customer_id: the customer id from the request.
    """
    c = get_db().one("SELECT address_reuse_accounts FROM customers WHERE id = %s", (customer_id,))
    if c is None:
        return {"error": "not_found", "hint": "unknown customer"}
    return {"customer_id": customer_id, "address_reuse_accounts": c["address_reuse_accounts"]}


def account_age(customer_id: str) -> dict:
    """Age of the customer's account in days.

    Args:
        customer_id: the customer id from the request.
    """
    c = get_db().one("SELECT created_at FROM customers WHERE id = %s", (customer_id,))
    if c is None:
        return {"error": "not_found", "hint": "unknown customer"}
    return {"customer_id": customer_id, "account_age_days": (_now() - parse_ts(c["created_at"])).days}


def payment_changes(customer_id: str) -> dict:
    """Number of payment method changes in the last 30 days.

    Args:
        customer_id: the customer id from the request.
    """
    c = get_db().one("SELECT payment_changes_30d FROM customers WHERE id = %s", (customer_id,))
    if c is None:
        return {"error": "not_found", "hint": "unknown customer"}
    return {"customer_id": customer_id, "payment_changes_30d": c["payment_changes_30d"]}
