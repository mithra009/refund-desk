"""Execute: the idempotent side effect. No model is invoked here.

Amount and order id come from validated state, not from model output at call time. The key is
stable across retries and replays and `refunds.idempotency_key` is UNIQUE, so a retry, a replay
from a checkpoint or a double-click on approve all return the same refund.
"""
from __future__ import annotations

from app import audit
from app.nodes.common import order_row
from app.state import CaseState
from backend_sim import payments


def execute(state: CaseState) -> dict:
    final = state["final"]
    order = order_row(state)                    # ownership re-checked against the session's customer
    if order is None or final is None or not final.outcome.startswith("approve"):
        raise RuntimeError("execute reached without an approvable decision and a owned order")
    key = f"{state['case_id']}:refund"
    refund_id, created = payments.issue_refund(order_id=order["id"], amount_cents=final.amount_cents,
                                               idempotency_key=key)
    audit.write(state["case_id"], "refund_issued" if created else "refund_replayed",
                {"refund_id": refund_id, "amount_cents": final.amount_cents, "order_id": order["id"]})
    return {"refund_id": refund_id}
