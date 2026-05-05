"""Supervisor: routing as code. The order of phases is fixed, so no model decides it.

The LLM-driven delegation is one level down, in the lead, where what to investigate
really does depend on the case.
"""
from __future__ import annotations

from typing import Literal

from langgraph.types import Command

from app.state import CaseState, GuardResult


def supervisor(state: CaseState) -> Command[Literal["lead_plan", "policy", "draft", "human_review"]]:
    budget = state["budget"]
    if budget.exhausted:
        return Command(goto="human_review", update={"guard": GuardResult.budget()})
    if not state.get("plan"):
        return Command(goto="lead_plan")
    if not state.get("policy_hits"):
        return Command(goto="policy")
    return Command(goto="draft")
