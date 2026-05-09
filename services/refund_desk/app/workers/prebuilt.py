"""Order, history and the single-investigator (ablation D) workers: prebuilt agent + middleware."""
from __future__ import annotations

from langchain.agents import create_agent
from langchain.agents.middleware import (ModelFallbackMiddleware, PIIMiddleware,
                                         ToolCallLimitMiddleware)
from langchain_core.messages import HumanMessage

from app import llm
from app.config import cfg
from app.redact import PHONE_DETECTOR
from app.state import WorkerInput
from app.tools.backend import toolset
from app.workers.common import failed_evidence, finalize, worker_system, worker_task

ROLES = {"order": "order investigator", "history": "customer-history investigator",
         "all": "single investigator with every read tool"}


def build_agent(customer_id: str, worker: str):
    return create_agent(
        model=llm.get_model("fast"),
        tools=toolset(customer_id, worker),
        system_prompt=worker_system(ROLES[worker]),
        middleware=[
            ToolCallLimitMiddleware(run_limit=cfg.max_worker_steps, exit_behavior="continue"),
            ModelFallbackMiddleware(llm.fallback_model("fast")),
            # PII never reaches the model from tool output.
            PIIMiddleware("email", strategy="redact", apply_to_input=False, apply_to_tool_results=True),
            PIIMiddleware("phone", detector=PHONE_DETECTOR, strategy="redact",
                          apply_to_input=False, apply_to_tool_results=True),
        ],
    )


def run_prebuilt_worker(inp: WorkerInput) -> dict:
    source = "investigator_worker" if inp["worker"] == "all" else f"{inp['worker']}_worker"
    try:
        agent = build_agent(inp["customer_id"], inp["worker"])
        result = agent.invoke({"messages": [HumanMessage(content=worker_task(inp))]},
                              config={"run_name": source, "recursion_limit": 4 * cfg.max_worker_steps + 6})
        ev, tokens = finalize(inp, source, result["messages"])
        return {"evidence": [ev], "spent": [{"tokens": tokens, "tool_calls": len(ev["raw"]["tool_results"])}]}
    except Exception as exc:
        return {"evidence": [failed_evidence(inp, source, exc)], "spent": [{}]}
