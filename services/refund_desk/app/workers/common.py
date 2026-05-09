"""Shared worker pieces: turning a message history into one Evidence item."""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app import llm
from app.nodes.common import msgs
from app.prompts.registry import get_prompt, render
from app.prompts.templates import wrap_data
from app.state import Evidence, EvidenceOut, WorkerInput
from app.config import cfg


def collect_tool_results(messages: list[BaseMessage]) -> list[dict]:
    """Pair each tool call with its result. These are the facts evidence rests on."""
    calls: dict[str, dict] = {}
    for m in messages:
        if isinstance(m, AIMessage):
            for c in m.tool_calls or []:
                calls[c["id"]] = {"tool": c["name"], "args": c["args"]}
    results = []
    for m in messages:
        if isinstance(m, ToolMessage):
            content: Any = m.content
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    pass
            results.append({**calls.get(m.tool_call_id, {"tool": m.name, "args": {}}), "result": content})
    return results


def tokens_in(messages: list[BaseMessage]) -> int:
    return sum(int((getattr(m, "usage_metadata", None) or {}).get("total_tokens", 0))
               for m in messages if isinstance(m, AIMessage))


def worker_system(role: str) -> str:
    return render("worker", role=role, today=cfg.now().date().isoformat())


def worker_task(inp: WorkerInput) -> str:
    return (f"Question: {inp['question']}\n"
            f"Order id: {inp['order_id'] or 'unknown (find it with your tools if you can)'}")


def finalize(inp: WorkerInput, source: str, messages: list[BaseMessage]) -> tuple[Evidence, int]:
    """One structured call: tool results -> Evidence. Facts come only from the tool results."""
    results = collect_tool_results(messages)
    out, tokens = llm.structured(
        "fast", EvidenceOut,
        msgs(get_prompt("finalize"),
             f"Question: {inp['question']}\n\n" + wrap_data("tool_results", results)),
        name=f"{source}_finalize")
    ev: Evidence = {
        "id": f"ev_{inp['worker']}_{inp['index']}",
        "source": source,
        "question": inp["question"],
        "finding": out.finding,
        "raw": {"tool_results": results},
        "confidence": out.confidence if results else min(out.confidence, 0.2),
    }
    return ev, tokens + tokens_in(messages)


def failed_evidence(inp: WorkerInput, source: str, exc: Exception) -> Evidence:
    """A worker that crashed becomes a zero-confidence item; the merge step re-dispatches once."""
    return {"id": f"ev_{inp['worker']}_{inp['index']}", "source": source, "question": inp["question"],
            "finding": f"worker failed: {type(exc).__name__}", "raw": {"error": type(exc).__name__},
            "confidence": 0.0}
