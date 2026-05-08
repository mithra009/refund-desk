"""Ablation configurations that are not just a switch on the full graph.

A. Single ReAct: one agent, every read tool, the whole policy stuffed into the prompt, sees the
   raw customer message, and acts on its own decision. No intake, guard, critic or human step.
B. Pipeline: the full graph with `lead_plan` replaced by a fixed sequence in code (no workers,
   no loop) and no critic. Built by graph.build_pipeline_graph.
C and D are `cfg.critic_enabled` / `cfg.hierarchy_enabled` on the full graph.
"""
from __future__ import annotations

import json

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app import audit, llm
from app.config import cfg
from app.nodes.common import budget_of, msgs
from app.prompts.templates import wrap_data
from app.retrieval import load_clauses
from app.risk_client import get_risk_client, trace_headers
from app.state import CaseState, Decision, Evidence, PlanItem
from app.tools.backend import build_tools, toolset
from app.workers.common import collect_tool_results, tokens_in
from backend_sim import payments
from backend_sim.db import get_db


# ---- B: fixed sequence in code ----------------------------------------------------
async def pipeline_gather(state: CaseState) -> Command:
    intake = state["intake"]
    tools = build_tools(state["customer_id"])
    order_tools = ["get_order", "get_order_items", "get_shipment", "get_tracking_events", "get_carrier_claim"]
    customer_tools = ["get_customer", "list_past_refunds", "list_past_cases"]
    evidence: list[Evidence] = []
    for name in order_tools + customer_tools:
        args = {"order_id": intake.order_id} if name in order_tools else {}
        result = tools[name].invoke(args)
        evidence.append({"id": f"ev_{name}", "source": "pipeline", "question": name,
                         "finding": json.dumps(result, default=str)[:600],
                         "raw": {"tool_results": [{"tool": name, "args": args, "result": result}]},
                         "confidence": 1.0})
    verdict = await get_risk_client().assess(state["customer_id"], intake.order_id, intake.claim_type,
                                             headers=trace_headers())
    if verdict is not None:
        evidence.append({"id": "ev_risk_0", "source": "risk_service", "question": "risk assessment",
                         "finding": f"Risk level {verdict.level} (score {verdict.score}); signals: "
                                    f"{', '.join(verdict.signals) or 'none'}.",
                         "raw": verdict.model_dump(), "confidence": 1.0})
    plan = [PlanItem(question=e["question"], worker="order") for e in evidence if e["source"] == "pipeline"]
    return Command(goto="supervisor", update={
        "plan": plan, "evidence": evidence, "risk": verdict,
        "budget": budget_of(state).spend(tool_calls=len(order_tools + customer_tools), steps=1)})


# ---- A: single ReAct agent ----------------------------------------------------------
def baseline_system_prompt() -> str:
    policy = "\n\n".join(f"{c.id} - {c.title}\n{c.text}" for c in load_clauses())
    return ("You resolve refund requests for an online store, end to end. Use your tools to look up the "
            "customer's orders, shipment and history, apply the policy below, and decide: approve_full, "
            "approve_partial, deny or escalate, with an amount in cents.\n\n"
            f"Today's date is {cfg.now().date().isoformat()}.\n\nPOLICY\n{policy}")


def run_baseline(case_id: str, customer_id: str, message: str, has_photo: bool = False) -> dict:
    """Config A. Returns {"decision", "executed", "refund_id", "tokens", "tool_calls"}."""
    agent = create_agent(
        model=llm.get_model("strong"),
        tools=toolset(customer_id, "baseline"),
        system_prompt=baseline_system_prompt(),
        middleware=[ToolCallLimitMiddleware(run_limit=cfg.max_tool_calls_per_case, exit_behavior="continue")],
    )
    text = f"{message}\n\n[photo attached: {'yes' if has_photo else 'no'}]"
    result = agent.invoke({"messages": [HumanMessage(content=text)]},
                          config={"run_name": "baseline_react", "recursion_limit": cfg.recursion_limit})
    history = result["messages"]
    results = collect_tool_results(history)
    decision, tokens = llm.structured(
        "strong", Decision,
        msgs("Return the refund decision you reached. Cite the policy clause ids you applied in `clauses` "
             "and the tool names you relied on in `evidence_refs`.",
             wrap_data("conversation", [str(m.content)[:1500] for m in history[-6:]])
             + wrap_data("tool_results", results)),
        name="baseline_decision")
    refund_id = None
    if decision.outcome.startswith("approve") and decision.amount_cents > 0:
        order = _find_order(customer_id, results)
        if order:
            refund_id, _ = payments.issue_refund(order, decision.amount_cents, f"{case_id}:refund",
                                                 actor="baseline_agent")
            audit.write(case_id, "refund_issued", {"refund_id": refund_id, "config": "A"})
    return {"decision": decision, "executed": refund_id is not None, "refund_id": refund_id,
            "tokens": tokens + tokens_in(history), "tool_calls": len(results), "tool_results": results}


def _find_order(customer_id: str, results: list[dict]) -> str | None:
    for r in results:
        oid = (r.get("args") or {}).get("order_id")
        if oid and get_db().one("SELECT id FROM orders WHERE id = %s AND customer_id = %s", (oid, customer_id)):
            return oid
    return None
