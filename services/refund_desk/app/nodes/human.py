"""Human review: a durable interrupt.

On resume this node restarts from its first line, so everything before `interrupt()` runs
twice. It therefore has no side effects before the interrupt, and the refund call is never
in this node (execute is separate and idempotent).
"""
from __future__ import annotations

from typing import Literal

from langgraph.types import Command, interrupt

from app import audit
from app.guards import final_decision_after_review
from app.nodes.common import order_total
from app.state import CaseState, HumanReview


def review_payload(state: CaseState) -> dict:
    d = state.get("draft")
    return {
        "case_id": state["case_id"],
        "draft": d.model_dump() if d else None,
        "reasons": state["guard"].reasons if state.get("guard") else [],
        "evidence": state.get("evidence", []),
        "summary": state["intake"].summary if state.get("intake") else None,
        "order_total_cents": order_total(state),
    }


def human_review(state: CaseState) -> Command[Literal["execute", "reply"]]:
    answer = interrupt(review_payload(state))                  # pauses; resumed by Command(resume=...)
    review = HumanReview.model_validate(answer)                # approve | edit | reject
    total = order_total(state) or 0
    final = final_decision_after_review(state.get("draft"), review.action, review.outcome,
                                        review.amount_cents, total)
    audit.write(state["case_id"], "human_review",
                {"action": review.action, "final": final.model_dump() if final else None, "note": review.note},
                actor=review.reviewer)
    goto = "execute" if final is not None and final.outcome.startswith("approve") else "reply"
    return Command(goto=goto, update={"human": review, "final": final})
