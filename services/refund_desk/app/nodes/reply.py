"""Customer reply: fast model, output checked in code.

The model is given the decision and a plain-language reason, never clause ids or the risk level.
The result must not mention internal clause ids or risk, and must not quote an amount other than
the one in `refunds.amount_cents`. After one failed retry a fixed template is used instead.
"""
from __future__ import annotations

from pydantic import BaseModel

from app import audit, llm
from app.guards import reply_violations
from app.nodes.common import budget_of, msgs
from app.prompts.registry import get_prompt
from app.prompts.templates import wrap_data
from app.state import CaseState
from backend_sim import payments

# Plain-language reasons for a denial, keyed by the clause that decided it.
PLAIN_REASON = {
    "R1": "the 30-day return window after delivery had already passed",
    "R2": "we could not confirm that the item was damaged (no carrier damage report or photo)",
    "R3": "the order is still inside its normal delivery window, so please allow a few more days",
    "R4": "the order is recorded as delivered",
    "R5": "the item was marked final sale",
    "R6": "the order was not delivered late or there was no shipping fee to return",
    "R10": "the order did not qualify for a change-of-mind return",
}


class ReplyText(BaseModel):
    text: str


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def template_reply(kind: str, amount_cents: int | None) -> str:
    if kind == "refunded":
        return (f"Good news: we have issued a refund of {money(amount_cents)} to your original payment "
                "method. It should arrive within 5 business days.")
    if kind == "denied":
        return "Thank you for getting in touch. After reviewing your order we are unable to approve a refund."
    return ("Thank you for getting in touch. A member of our team is reviewing your request and "
            "will get back to you.")


def reply(state: CaseState) -> dict:
    final = state.get("final")
    human = state.get("human")
    refund = payments.get_refund(f"{state['case_id']}:refund") if state.get("refund_id") else None
    if refund is not None:
        kind, amount = "refunded", int(refund["amount_cents"])
    elif final is not None and final.outcome == "deny":
        kind, amount = "denied", None
    elif human is not None and human.action == "reject":
        kind, amount = "denied", None
    else:
        kind, amount = "pending", None

    context = {"decision": kind, "refund_amount": money(amount) if amount is not None else None,
               "claim_summary": state["intake"].summary if state.get("intake") else None}
    if kind == "denied" and final is not None:
        context["reason"] = "; ".join(PLAIN_REASON[c] for c in final.clauses if c in PLAIN_REASON) or None

    tokens, text, feedback = 0, None, ""
    for _ in range(2):
        out, t = llm.structured("fast", ReplyText,
                                msgs(get_prompt("reply"), wrap_data("decision", context) + feedback),
                                name="reply")
        tokens += t
        problems = reply_violations(out.text, amount)
        if not problems:
            text = out.text
            break
        feedback = "\n\nYour previous reply was rejected: " + "; ".join(problems) + ". Write it again."
    if text is None:
        text = template_reply(kind, amount)
        audit.write(state["case_id"], "reply_template_used", {"problems": problems})
    audit.write(state["case_id"], "reply", {"kind": kind, "text": text})
    return {"reply": text, "budget": budget_of(state).spend(tokens=tokens, steps=1)}
