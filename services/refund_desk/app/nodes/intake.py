"""Intake: one structured call. No tools, no loop.

The only node that reads the raw customer message. Everything downstream sees the
structured summary.
"""
from __future__ import annotations

from typing import Literal

from langgraph.types import Command

from app import audit, llm
from app.nodes.common import budget_of, msgs
from app.prompts.registry import get_prompt
from app.prompts.templates import wrap_data
from app.state import CaseState, GuardResult, Intake
from backend_sim.db import get_db


def _latest_open_order(customer_id: str) -> str | None:
    row = get_db().one("SELECT id FROM orders WHERE customer_id = %s AND status != 'refunded'"
                       " ORDER BY placed_at DESC LIMIT 1", (customer_id,))
    return row["id"] if row else None


def intake(state: CaseState) -> Command[Literal["supervisor", "human_review"]]:
    parsed, tokens = llm.structured(
        "fast", Intake, msgs(get_prompt("intake"), wrap_data("customer_message", state["message"])),
        name="intake")
    customer_id = state["customer_id"]
    db = get_db()

    # An order id the model read from the message is only a candidate: it must exist and
    # belong to this customer, else it is discarded and looked up in code.
    order_id = parsed.order_id
    if order_id and db.one("SELECT id FROM orders WHERE id = %s AND customer_id = %s", (order_id, customer_id)) is None:
        order_id = None
    if order_id is None:
        order_id = _latest_open_order(customer_id)

    # "Customer supplied a photo" (R2) is a fact about the upload, not about what the text claims.
    parsed = parsed.model_copy(update={"order_id": order_id, "has_photo": bool(state.get("has_photo"))})
    audit.write(state["case_id"], "intake", {"claim_type": parsed.claim_type, "order_id": order_id,
                                             "summary": parsed.summary})
    update = {"intake": parsed, "budget": budget_of(state).spend(tokens=tokens, steps=1)}
    if parsed.claim_type == "other" or order_id is None:
        reason = "unclassifiable" if parsed.claim_type == "other" else "order_not_found"
        update["guard"] = GuardResult(reasons=[reason])
        return Command(goto="human_review", update=update)
    return Command(goto="supervisor", update=update)
