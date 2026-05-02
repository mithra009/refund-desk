"""Decision draft: strong model, structured output -> Decision."""
from __future__ import annotations

from app import audit, llm
from app.config import cfg
from app.nodes.common import budget_of, msgs, order_total, usable_evidence
from app.prompts.registry import render
from app.prompts.templates import wrap_data
from app.state import CaseState, Decision


def render_case(state: CaseState) -> str:
    """The context a decision is made from. Facts and clauses; never the raw customer message."""
    intake = state["intake"]
    evidence = [{"id": e["id"], "source": e["source"], "question": e["question"],
                 "finding": e["finding"], "confidence": e["confidence"]}
                for e in usable_evidence(state.get("evidence", []))]
    parts = [
        wrap_data("claim", {"claim_type": intake.claim_type, "summary": intake.summary,
                            "order_id": intake.order_id,
                            "customer_requested_amount_cents": intake.requested_amount_cents,
                            "photo_supplied": intake.has_photo}),
        wrap_data("evidence", evidence),
        wrap_data("risk_assessment", state["risk"].model_dump() if state.get("risk") else
                  "UNAVAILABLE: treat as unknown; a human must review"),
        wrap_data("policy_clauses", [{"id": c.id, "title": c.title, "text": c.text}
                                     for c in state.get("policy_hits", [])]),
    ]
    total = order_total(state)
    if total is not None:
        parts.append(wrap_data("order_total_cents", str(total)))
    return "\n\n".join(parts)


def draft(state: CaseState) -> dict:
    prompt = render("decide", today=cfg.now().date().isoformat())
    human = render_case(state)
    critiques = state.get("critiques", [])
    if state.get("draft") is not None and critiques and critiques[-1].verdict == "revise":
        human += ("\n\n" + wrap_data("previous_draft", state["draft"].model_dump())
                  + "\n\n" + wrap_data("reviewer_issues", [i.model_dump() for i in critiques[-1].issues])
                  + "\n\nRevise the previous draft to fix every issue. Return a complete decision.")
    decision, tokens = llm.structured("strong", Decision, msgs(prompt, human), name="draft")
    audit.write(state["case_id"], "draft", {"revision": state.get("revision_count", 0),
                                            "decision": decision.model_dump()})
    return {"draft": decision, "budget": budget_of(state).spend(tokens=tokens, steps=1)}
