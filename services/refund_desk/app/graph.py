"""Top-level StateGraph wiring.

START -> intake -> supervisor -> {lead_plan | policy | draft | human_review}
lead_plan -Send-> workers + risk_check -> evidence_merge -> supervisor
draft -> critic -> {draft | guard} ; guard -> {execute | human_review | reply}
human_review -> {execute | reply} ; execute -> reply -> close_case -> END
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.nodes.common import MODEL_RETRY
from app.nodes.close import close_case
from app.nodes.critic import critic
from app.nodes.decide import draft
from app.nodes.execute import execute
from app.nodes.guard import guard
from app.nodes.human import human_review
from app.nodes.intake import intake
from app.nodes.lead import lead_plan
from app.nodes.merge import evidence_merge
from app.nodes.policy import policy
from app.nodes.reply import reply
from app.nodes.risk import risk_check
from app.nodes.supervisor import supervisor
from app.state import CaseState, WorkerInput
from app.workers.history import history_worker
from app.workers.order import order_worker
from app.workers.prebuilt import run_prebuilt_worker
from app.workers.shipment import run_shipment_worker

NET_RETRY = RetryPolicy(max_attempts=2, initial_interval=0.5, backoff_factor=2.0, jitter=True)


def build_graph(checkpointer=None, store=None, variant: str = "full"):
    """variant="full" is the design in IMPLEMENTATION.md. variant="pipeline" is ablation config B:
    a fixed sequence (code gathers evidence, no workers loop, no critic)."""
    if variant == "pipeline":
        return build_pipeline_graph(checkpointer, store)
    g = StateGraph(CaseState)

    g.add_node("intake", intake, retry_policy=MODEL_RETRY)
    g.add_node("supervisor", supervisor)
    g.add_node("lead_plan", lead_plan, retry_policy=MODEL_RETRY)
    # Workers take a private input state, not CaseState: one question and ids.
    g.add_node("order_worker", order_worker, input_schema=WorkerInput)
    g.add_node("shipment_worker", run_shipment_worker, input_schema=WorkerInput)
    g.add_node("history_worker", history_worker, input_schema=WorkerInput)
    g.add_node("investigator_worker", run_prebuilt_worker, input_schema=WorkerInput)
    g.add_node("risk_check", risk_check)
    g.add_node("evidence_merge", evidence_merge)
    g.add_node("policy", policy)
    g.add_node("draft", draft, retry_policy=MODEL_RETRY)
    g.add_node("critic", critic, retry_policy=MODEL_RETRY)
    g.add_node("guard", guard)
    g.add_node("human_review", human_review)
    g.add_node("execute", execute)                 # no retry policy: idempotency covers replays
    g.add_node("reply", reply, retry_policy=MODEL_RETRY)
    g.add_node("close_case", close_case)

    g.add_edge(START, "intake")
    for worker in ("order_worker", "shipment_worker", "history_worker", "investigator_worker", "risk_check"):
        g.add_edge(worker, "evidence_merge")
    g.add_edge("policy", "supervisor")
    g.add_edge("draft", "critic")
    g.add_edge("execute", "reply")
    g.add_edge("reply", "close_case")
    g.add_edge("close_case", END)
    # intake, supervisor, lead_plan, evidence_merge, critic, guard, human_review route with Command.

    return g.compile(checkpointer=checkpointer, store=store)


def build_pipeline_graph(checkpointer=None, store=None):
    from app.variants import pipeline_gather
    g = StateGraph(CaseState)
    g.add_node("intake", intake, retry_policy=MODEL_RETRY)
    g.add_node("supervisor", supervisor)
    g.add_node("lead_plan", pipeline_gather)       # same slot, no model and no loop
    g.add_node("policy", policy)
    g.add_node("draft", draft, retry_policy=MODEL_RETRY)
    g.add_node("guard", guard)
    g.add_node("human_review", human_review)
    g.add_node("execute", execute)
    g.add_node("reply", reply, retry_policy=MODEL_RETRY)
    g.add_node("close_case", close_case)
    g.add_edge(START, "intake")
    g.add_edge("policy", "supervisor")
    g.add_edge("draft", "guard")
    g.add_edge("execute", "reply")
    g.add_edge("reply", "close_case")
    g.add_edge("close_case", END)
    return g.compile(checkpointer=checkpointer, store=store)
