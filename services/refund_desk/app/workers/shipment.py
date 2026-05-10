"""Shipment worker, hand-built ReAct so the loop is visible in the code and the trace.

The order and history workers use the prebuilt agent constructor plus middleware
(workers/prebuilt.py); this one shows what that constructor does underneath.
"""
from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app import llm
from app.config import cfg
from app.state import Evidence, WorkerInput
from app.tools.backend import toolset
from app.workers.common import failed_evidence, finalize, worker_system, worker_task


class WorkerState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    steps: int
    inp: WorkerInput
    evidence: Evidence | None
    tokens: int


def build_shipment_graph(customer_id: str):
    tools = toolset(customer_id, "shipment")

    def call_model(state: WorkerState):
        try:
            msg = llm.get_model("fast").bind_tools(tools).invoke(state["messages"])
        except Exception:      # provider outage: one hop to the fallback model
            msg = llm.fallback_model("fast").bind_tools(tools).invoke(state["messages"])
        return {"messages": [msg], "steps": state["steps"] + 1}

    def route(state: WorkerState) -> Literal["tools", "finalize"]:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None) and state["steps"] < cfg.max_worker_steps:
            return "tools"
        return "finalize"

    def to_evidence(state: WorkerState):
        ev, tokens = finalize(state["inp"], "shipment_worker", state["messages"])
        return {"evidence": ev, "tokens": tokens}

    g = StateGraph(WorkerState)
    g.add_node("model", call_model)
    g.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    g.add_node("finalize", to_evidence)       # structured call: messages -> Evidence
    g.add_edge(START, "model")
    g.add_conditional_edges("model", route)
    g.add_edge("tools", "model")
    g.add_edge("finalize", END)
    return g.compile()


def run_shipment_worker(inp: WorkerInput) -> dict:
    """Parent-graph node body. Receives a private input, returns only distilled evidence."""
    try:
        out = build_shipment_graph(inp["customer_id"]).invoke(
            {"messages": [SystemMessage(content=worker_system("shipment investigator")),
                          HumanMessage(content=worker_task(inp))],
             "steps": 0, "inp": inp, "evidence": None, "tokens": 0},
            config={"run_name": "shipment_worker", "recursion_limit": 4 * cfg.max_worker_steps + 6})
        ev = out["evidence"]
        calls = len(ev["raw"].get("tool_results", []))
        return {"evidence": [ev], "spent": [{"tokens": out["tokens"], "tool_calls": calls}]}
    except Exception as exc:
        return {"evidence": [failed_evidence(inp, "shipment_worker", exc)], "spent": [{}]}
