"""Evidence merge: runs once all parallel branches finish.

Folds worker spend into the budget, checks every plan item got a usable answer, re-dispatches
the unanswered ones once, and records what is still missing as `unknown` evidence, which pushes
the case towards escalation (the guard treats unknown evidence as a reason for a human).
"""
from __future__ import annotations

from langgraph.types import Command

from app import audit
from app.config import cfg
from app.nodes.common import budget_of
from app.nodes.lead import dispatch
from app.state import CaseState, Evidence


def unanswered(state: CaseState) -> set[int]:
    good = {e["question"] for e in state.get("evidence", [])
            if e["confidence"] >= cfg.evidence_conf_floor and not e["raw"].get("unknown")}
    return {i for i, it in enumerate(state["plan"]) if it.question not in good}


def evidence_merge(state: CaseState) -> Command:
    spent = state.get("spent", [])
    folded = state.get("spent_folded", 0)
    fresh = spent[folded:]
    budget = budget_of(state).spend(tokens=sum(s.get("tokens", 0) for s in fresh),
                                    tool_calls=sum(s.get("tool_calls", 0) for s in fresh))
    update: dict = {"budget": budget, "spent_folded": len(spent)}
    missing = unanswered(state)

    if missing and not state.get("redispatched") and not budget.exhausted:
        update["redispatched"] = True
        audit.write(state["case_id"], "redispatch", {"items": sorted(missing)})
        return Command(goto=dispatch(state["plan"], state, only=missing), update=update)

    unknowns: list[Evidence] = [
        {"id": f"ev_unknown_{i}", "source": "merge", "question": state["plan"][i].question,
         "finding": "UNKNOWN: no usable evidence could be gathered for this question.",
         "raw": {"unknown": True}, "confidence": 0.0}
        for i in sorted(missing)]
    if unknowns:
        update["evidence"] = unknowns
        audit.write(state["case_id"], "evidence_unknown", {"items": sorted(missing)})
    return Command(goto="supervisor", update=update)
