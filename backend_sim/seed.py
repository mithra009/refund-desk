"""Seed the fake backend from fact sheets.

    python -m backend_sim.seed                # demo customers
    python -m backend_sim.seed scenarios.jsonl
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

from backend_sim.db import Db, get_db
from backend_sim.factsheet import AS_OF, EventFact, FactSheet, ItemFact


def seed_fact_sheet(db: Db, f: FactSheet, as_of=AS_OF) -> None:
    """Idempotent: re-seeding the same scenario replaces its rows."""
    created = as_of - timedelta(days=f.account_age_days)
    tracking_no = f"TRK-{f.order_id}"
    db.execute("DELETE FROM refunds WHERE order_id LIKE %s", (f"{f.order_id}%",))
    db.execute("DELETE FROM order_items WHERE order_id = %s", (f.order_id,))
    db.execute("DELETE FROM shipments WHERE order_id = %s", (f.order_id,))
    db.execute("DELETE FROM tracking_events WHERE tracking_no = %s", (tracking_no,))
    db.execute("DELETE FROM carrier_claims WHERE tracking_no = %s", (tracking_no,))
    db.execute("DELETE FROM orders WHERE id LIKE %s", (f"{f.order_id}%",))
    db.execute("DELETE FROM cases WHERE customer_id = %s", (f.customer_id,))
    db.execute("DELETE FROM customers WHERE id = %s", (f.customer_id,))
    db.execute(
        "INSERT INTO customers (id, created_at, tier, lifetime_orders, lifetime_refunds,"
        " payment_changes_30d, address_reuse_accounts) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (f.customer_id, db.ts(created), f.tier, f.lifetime_orders, f.lifetime_refunds,
         f.payment_changes_30d, f.address_reuse_accounts))
    delivered = f.delivered_event
    db.execute(
        "INSERT INTO orders (id, customer_id, placed_at, total_cents, shipping_cents, status)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (f.order_id, f.customer_id, db.ts(as_of - timedelta(days=f.placed_days_ago)),
         f.total_cents, f.shipping_cents, "delivered" if delivered else "shipped"))
    for it in f.items:
        db.execute("INSERT INTO order_items (order_id, sku, price_cents, final_sale, note) VALUES (%s,%s,%s,%s,%s)",
                   (f.order_id, it.sku, it.price_cents, bool(it.final_sale), it.note))
    db.execute(
        "INSERT INTO shipments (order_id, carrier, tracking_no, promised_at, delivered_at)"
        " VALUES (%s,%s,%s,%s,%s)",
        (f.order_id, f.carrier, tracking_no, db.ts(as_of - timedelta(days=f.promised_days_ago)),
         db.ts(delivered.ts(as_of)) if delivered else None))
    for e in f.events:
        db.execute("INSERT INTO tracking_events (tracking_no, ts, code) VALUES (%s,%s,%s)",
                   (tracking_no, db.ts(e.ts(as_of)), e.code))
        if e.code == "DAMAGED_SCAN":
            db.execute("INSERT INTO carrier_claims (tracking_no, status, opened_at) VALUES (%s,%s,%s)",
                       (tracking_no, "open", db.ts(e.ts(as_of))))
    # history: past refunds inside the 90-day window
    for i in range(f.refunds_last_90d):
        oid = f"{f.order_id}-past{i}"
        db.execute("INSERT INTO orders (id, customer_id, placed_at, total_cents, shipping_cents, status)"
                   " VALUES (%s,%s,%s,%s,%s,%s)",
                   (oid, f.customer_id, db.ts(as_of - timedelta(days=30 + 10 * i)), 3000, 0, "refunded"))
        db.execute("INSERT INTO refunds (id, order_id, amount_cents, idempotency_key, created_at, actor)"
                   " VALUES (%s,%s,%s,%s,%s,%s)",
                   (f"re_{oid}", oid, 3000, f"seed:{oid}", db.ts(as_of - timedelta(days=20 + 10 * i)), "seed"))
    if f.prior_missing_approved:
        oid = f"{f.order_id}-priormissing"
        db.execute("INSERT INTO orders (id, customer_id, placed_at, total_cents, shipping_cents, status)"
                   " VALUES (%s,%s,%s,%s,%s,%s)",
                   (oid, f.customer_id, db.ts(as_of - timedelta(days=200)), 2500, 0, "refunded"))
        db.execute(
            "INSERT INTO cases (id, thread_id, customer_id, order_id, claim_type, status, created_at, final_decision)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (f"seed-{f.customer_id}-r4", f"seed-{f.customer_id}-r4", f.customer_id, oid,
             "missing_after_delivery", "closed", db.ts(as_of - timedelta(days=199)),
             db.json({"outcome": "approve_full", "amount_cents": 2500, "clauses": ["R4"]})))


def seed_demo(db: Db) -> list[FactSheet]:
    """Three customers for the scripted demo: auto-approve, escalate-for-amount, deny."""
    sheets = [
        FactSheet(scenario_id="demo-1", customer_id="cus_demo_1", order_id="ord_demo_1",
                  items=[ItemFact(sku="MUG-RED", price_cents=2400)], shipping_cents=500,
                  events=[EventFact(days_ago=10, code="IN_TRANSIT"), EventFact(days_ago=5, code="DELIVERED"),
                          EventFact(days_ago=5, code="DAMAGED_SCAN")],
                  promised_days_ago=6, claim_type="damaged"),
        FactSheet(scenario_id="demo-2", customer_id="cus_demo_2", order_id="ord_demo_2",
                  items=[ItemFact(sku="CHAIR-ERGO", price_cents=24900)], shipping_cents=1500,
                  events=[EventFact(days_ago=9, code="IN_TRANSIT"), EventFact(days_ago=4, code="DELIVERED"),
                          EventFact(days_ago=4, code="DAMAGED_SCAN")],
                  promised_days_ago=5, claim_type="damaged"),
        FactSheet(scenario_id="demo-3", customer_id="cus_demo_3", order_id="ord_demo_3",
                  items=[ItemFact(sku="LAMP-DESK", price_cents=4500)], shipping_cents=500,
                  events=[EventFact(days_ago=40, code="IN_TRANSIT"), EventFact(days_ago=38, code="DELIVERED")],
                  promised_days_ago=39, claim_type="changed_mind"),
    ]
    for s in sheets:
        seed_fact_sheet(db, s)
    return sheets


def seed_jsonl(db: Db, path: Path) -> int:
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            seed_fact_sheet(db, FactSheet.model_validate(json.loads(line)["facts"]))
            n += 1
    return n


if __name__ == "__main__":
    d = get_db()
    if len(sys.argv) > 1:
        print(f"seeded {seed_jsonl(d, Path(sys.argv[1]))} scenarios")
    else:
        print(f"seeded {len(seed_demo(d))} demo customers")
