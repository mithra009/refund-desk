"""Read-only backend tools for the evidence workers.

`customer_id` is bound when the toolset is built (from the authenticated session), so it is
not a tool argument and the model cannot supply or change it. An order that does not belong
to that customer is indistinguishable from one that does not exist.
No tool here changes anything.
"""
from __future__ import annotations

from datetime import timedelta

from langchain_core.tools import BaseTool, tool

from app import memory
from app.config import cfg
from backend_sim.db import Db, get_db, parse_ts

NOT_FOUND = {"error": "not_found", "hint": "check order id"}


def _iso(v) -> str | None:
    d = parse_ts(v)
    return d.isoformat() if d else None


def _owned_order(db: Db, customer_id: str, order_id: str | None) -> dict | None:
    if not order_id:
        return None
    return db.one("SELECT * FROM orders WHERE id = %s AND customer_id = %s", (order_id, customer_id))


def build_tools(customer_id: str) -> dict[str, BaseTool]:
    """All read tools, bound to one customer. Workers pick a subset by name."""

    @tool
    def get_order(order_id: str) -> dict:
        """Look up one order: status, total, shipping fee and placement date."""
        db = get_db()
        o = _owned_order(db, customer_id, order_id)
        if o is None:
            return NOT_FOUND
        return {"order_id": o["id"], "status": o["status"], "total_cents": o["total_cents"],
                "shipping_cents": o["shipping_cents"], "placed_at": _iso(o["placed_at"])}

    @tool
    def get_order_items(order_id: str) -> dict:
        """List the items on an order with price and whether each is final sale."""
        db = get_db()
        if _owned_order(db, customer_id, order_id) is None:
            return NOT_FOUND
        rows = db.all("SELECT sku, price_cents, final_sale, note FROM order_items WHERE order_id = %s",
                      (order_id,))
        return {"order_id": order_id, "items": [
            {"sku": r["sku"], "price_cents": r["price_cents"], "final_sale": bool(r["final_sale"]),
             "note": r["note"]} for r in rows]}

    @tool
    def get_shipment(order_id: str) -> dict:
        """Carrier, tracking number, promised delivery date and delivery date for an order."""
        db = get_db()
        if _owned_order(db, customer_id, order_id) is None:
            return NOT_FOUND
        s = db.one("SELECT * FROM shipments WHERE order_id = %s", (order_id,))
        if s is None:
            return {"error": "not_found", "hint": "order has no shipment yet"}
        promised, delivered, now = parse_ts(s["promised_at"]), parse_ts(s["delivered_at"]), cfg.now()
        out = {"order_id": order_id, "carrier": s["carrier"], "tracking_no": s["tracking_no"],
               "promised_at": _iso(promised), "delivered_at": _iso(delivered),
               "today": now.isoformat(), "days_past_promised_today": round((now - promised).total_seconds() / 86400, 1)}
        if delivered:
            out["days_since_delivery"] = round((now - delivered).total_seconds() / 86400, 1)
            out["days_late"] = round(max(0.0, (delivered - promised).total_seconds() / 86400), 1)
        return out

    @tool
    def get_tracking_events(order_id: str) -> dict:
        """Carrier tracking events for an order (IN_TRANSIT, DELIVERED, DAMAGED_SCAN, LOST), oldest first."""
        db = get_db()
        if _owned_order(db, customer_id, order_id) is None:
            return NOT_FOUND
        s = db.one("SELECT tracking_no FROM shipments WHERE order_id = %s", (order_id,))
        if s is None:
            return {"error": "not_found", "hint": "order has no shipment yet"}
        rows = db.all("SELECT ts, code FROM tracking_events WHERE tracking_no = %s ORDER BY ts", (s["tracking_no"],))
        return {"tracking_no": s["tracking_no"], "events": [{"ts": _iso(r["ts"]), "code": r["code"]} for r in rows]}

    @tool
    def get_carrier_claim(order_id: str) -> dict:
        """Whether the carrier opened a damage or loss claim for this order's parcel."""
        db = get_db()
        if _owned_order(db, customer_id, order_id) is None:
            return NOT_FOUND
        s = db.one("SELECT tracking_no FROM shipments WHERE order_id = %s", (order_id,))
        c = db.one("SELECT status, opened_at FROM carrier_claims WHERE tracking_no = %s",
                   (s["tracking_no"],)) if s else None
        return {"claim": None} if c is None else {"claim": {"status": c["status"], "opened_at": _iso(c["opened_at"])}}

    @tool
    def get_customer() -> dict:
        """The authenticated customer's profile: tier, account age and lifetime counts."""
        db = get_db()
        c = db.one("SELECT * FROM customers WHERE id = %s", (customer_id,))
        if c is None:
            return {"error": "not_found", "hint": "customer unknown"}
        age = (cfg.now() - parse_ts(c["created_at"])).days
        return {"tier": c["tier"], "account_age_days": age, "lifetime_orders": c["lifetime_orders"],
                "lifetime_refunds": c["lifetime_refunds"]}

    @tool
    def list_past_refunds() -> dict:
        """The customer's refunds in the last 90 days, with a count."""
        db = get_db()
        since = cfg.now() - timedelta(days=90)
        rows = db.all("SELECT r.id, r.order_id, r.amount_cents, r.created_at FROM refunds r"
                      " JOIN orders o ON o.id = r.order_id WHERE o.customer_id = %s ORDER BY r.created_at",
                      (customer_id,))
        recent = [r for r in rows if parse_ts(r["created_at"]) >= since]
        return {"window_days": 90, "count": len(recent), "refunds": [
            {"id": r["id"], "order_id": r["order_id"], "amount_cents": r["amount_cents"],
             "created_at": _iso(r["created_at"])} for r in recent]}

    @tool
    def list_past_cases() -> dict:
        """The customer's earlier refund cases with claim type and outcome."""
        db = get_db()
        rows = db.all("SELECT id, claim_type, status, created_at, final_decision FROM cases"
                      " WHERE customer_id = %s ORDER BY created_at", (customer_id,))
        out = []
        for r in rows:
            fd = db.parse_json(r["final_decision"]) or {}
            out.append({"case_id": r["id"], "claim_type": r["claim_type"], "status": r["status"],
                        "outcome": fd.get("outcome"), "amount_cents": fd.get("amount_cents"),
                        "created_at": _iso(r["created_at"])})
        return {"cases": out}

    @tool
    def get_customer_memory() -> dict:
        """Short factual notes from this customer's previous cases (long-term memory)."""
        return {"notes": memory.read_memory(customer_id)}

    @tool
    def list_recent_orders() -> dict:
        """The customer's most recent orders, newest first. Use when the order id is not known."""
        rows = get_db().all("SELECT id, status, total_cents, placed_at FROM orders WHERE customer_id = %s"
                            " ORDER BY placed_at DESC LIMIT 5", (customer_id,))
        return {"orders": [{"order_id": r["id"], "status": r["status"], "total_cents": r["total_cents"],
                            "placed_at": _iso(r["placed_at"])} for r in rows]}

    tools = [list_recent_orders, get_order, get_order_items, get_shipment, get_tracking_events, get_carrier_claim,
             get_customer, list_past_refunds, list_past_cases, get_customer_memory]
    return {t.name: t for t in tools}


WORKER_TOOLS = {
    "order": ["get_order", "get_order_items"],
    "shipment": ["get_shipment", "get_tracking_events", "get_carrier_claim"],
    "history": ["get_customer", "list_past_refunds", "list_past_cases", "get_customer_memory"],
}
ALL_READ_TOOLS = [n for names in WORKER_TOOLS.values() for n in names]
BASELINE_TOOLS = ["list_recent_orders"] + ALL_READ_TOOLS     # config A has no intake step to find the order


def toolset(customer_id: str, worker: str) -> list[BaseTool]:
    tools = build_tools(customer_id)
    names = {"all": ALL_READ_TOOLS, "baseline": BASELINE_TOOLS}.get(worker) or WORKER_TOOLS[worker]
    return [tools[n] for n in names]
