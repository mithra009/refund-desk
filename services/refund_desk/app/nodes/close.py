"""close_case: persist the outcome and write long-term memory. No model."""
from __future__ import annotations

from datetime import datetime, timezone

from app import audit, memory
from app.state import CaseState
from backend_sim.db import get_db


def status_of(state: CaseState) -> str:
    if state.get("refund_id"):
        return "refunded"
    if state.get("human") is not None:
        return "reviewed"
    return "closed"


def close_case(state: CaseState) -> dict:
    db = get_db()
    final = state.get("final")
    intake = state.get("intake")
    status = status_of(state)
    decision = final.model_dump() if final else {"outcome": "rejected" if state.get("human") else "none"}
    db.execute("DELETE FROM cases WHERE id = %s", (state["case_id"],))
    db.execute(
        "INSERT INTO cases (id, thread_id, customer_id, order_id, claim_type, status, created_at, final_decision)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (state["case_id"], state["case_id"], state["customer_id"], intake.order_id if intake else None,
         intake.claim_type if intake else None, status, db.ts(datetime.now(timezone.utc)), db.json(decision)))
    if final is not None:
        memory.write_memory(state["customer_id"], state["case_id"],
                            f"{intake.claim_type if intake else 'unknown'} claim: {final.outcome}, "
                            f"{final.amount_cents} cents")
    audit.write(state["case_id"], "closed", {"status": status})
    return {}
