"""Investigation lead: plan, then fan out.

Writes an explicit list of evidence questions first (so the fan-out is inspectable and can be
scored), then dispatches one worker per question in parallel via `Send`, plus the risk check.
With hierarchy off (ablation D) one investigator with every read tool replaces lead + workers.
"""
from __future__ import annotations

from langgraph.types import Command, Send

from app import llm
from app.config import cfg
from app.nodes.common import budget_of, msgs
from app.prompts.registry import render
from app.prompts.templates import wrap_data
from app.state import CaseState, Plan, PlanItem

WORKER_NODE = {"order": "order_worker", "shipment": "shipment_worker", "history": "history_worker",
               "all": "investigator_worker"}


def default_plan(claim_type: str) -> list[PlanItem]:
    """Code fallback when the planner returns nothing usable."""
    items = [PlanItem(question="What are the order total, shipping fee and items, and which are final sale?",
                      worker="order"),
             PlanItem(question="How many refunds has this customer had in the last 90 days, and have they had "
                               "an earlier missing-parcel approval?", worker="history")]
    if claim_type != "changed_mind":
        items.insert(1, PlanItem(question="What are the promised and delivered dates and the tracking events "
                                          "(including any damage or loss scan)?", worker="shipment"))
    else:
        items.insert(1, PlanItem(question="When was the order delivered?", worker="shipment"))
    return items


def worker_input(item: PlanItem, index: int, state: CaseState) -> dict:
    """Private worker input: one question and ids. Never the customer's message."""
    return {"question": item.question, "order_id": state["intake"].order_id,
            "customer_id": state["customer_id"], "worker": item.worker, "index": index}


def dispatch(items: list[PlanItem], state: CaseState, only: set[int] | None = None) -> list[Send]:
    return [Send(WORKER_NODE[it.worker], worker_input(it, i, state))
            for i, it in enumerate(items) if only is None or i in only]


def risk_send(state: CaseState) -> Send:
    return Send("risk_check", {"customer_id": state["customer_id"], "order_id": state["intake"].order_id,
                               "claim_type": state["intake"].claim_type})


def lead_plan(state: CaseState) -> Command:
    intake = state["intake"]
    budget, tokens = budget_of(state), 0
    if not cfg.hierarchy_enabled:
        items = [PlanItem(worker="all", question=(
            "Gather everything needed to decide this refund claim: order total, shipping fee, items and "
            "final-sale flags; promised and delivered dates and tracking events; the customer's refunds in "
            "the last 90 days and any earlier missing-parcel approval."))]
    else:
        plan, tokens = llm.structured(
            "strong", Plan,
            msgs(render("plan", max_items=cfg.max_plan_items, today=cfg.now().date().isoformat()),
                 wrap_data("claim", {"claim_type": intake.claim_type, "summary": intake.summary,
                                     "order_id": intake.order_id})),
            name="lead_plan")
        items = plan.items[: cfg.max_plan_items] or default_plan(intake.claim_type)
    sends = dispatch(items, state) + [risk_send(state)]
    return Command(goto=sends, update={"plan": items, "redispatched": False, "spent_folded": 0,
                                       "budget": budget.spend(tokens=tokens, steps=1)})
