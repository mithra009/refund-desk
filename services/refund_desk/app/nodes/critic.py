"""Critic: grounded self-reflection.

1. Cheap checks in code first. If they fail the draft goes back with the error and no critic
   tokens are spent.
2. A rubric critic (four yes/no checks) that sees the draft, the clauses and the evidence, but
   not the drafter's reasoning.
3. Hard cap on revisions; at the cap the case moves on to the guard with the issues still open,
   which forces escalation.
With the critic switched off (ablation C) the node passes straight through to the guard.
"""
from __future__ import annotations

from typing import Literal

from langgraph.types import Command

from app import llm
from app.config import cfg
from app.guards import cheap_checks
from app.nodes.common import budget_of, msgs, order_total, usable_evidence
from app.prompts.registry import get_prompt
from app.prompts.templates import wrap_data
from app.state import CaseState, Critique, Issue


def render_critic_input(state: CaseState) -> str:
    d = state["draft"]
    return "\n\n".join([
        wrap_data("draft_decision", d.model_dump()),
        wrap_data("policy_clauses", [{"id": c.id, "title": c.title, "text": c.text}
                                     for c in state.get("policy_hits", [])]),
        wrap_data("evidence", [{"id": e["id"], "finding": e["finding"]}
                               for e in usable_evidence(state.get("evidence", []))]),
        wrap_data("order_total_cents", str(order_total(state))),
    ])


def _next(state: CaseState, critique: Critique) -> Command:
    count = state.get("revision_count", 0)
    update = {"critiques": [critique]}
    if critique.verdict == "accept" or count >= cfg.max_revisions:
        return Command(goto="guard", update=update)
    update["revision_count"] = count + 1
    return Command(goto="draft", update=update)


def critic(state: CaseState) -> Command[Literal["draft", "guard"]]:
    if not cfg.critic_enabled:
        return Command(goto="guard")

    errors = cheap_checks(state["draft"], state.get("policy_hits", []), state.get("evidence", []),
                          order_total(state))
    if errors:
        flags = {"clauses_exist": not any("clause" in e for e in errors),
                 "claims_grounded": not any("evidence id" in e for e in errors),
                 "amount_follows": not any(k in e for e in errors for k in ("amount", "approv", "approve_full"))}
        return _next(state, Critique(
            verdict="revise", source="code", nothing_ignored=True, **flags,
            issues=[Issue(kind="other", detail=e, fix="correct this field") for e in errors]))

    c, tokens = llm.structured("critic", Critique, msgs(get_prompt("critic"), render_critic_input(state)),
                               name="critic")
    c = c.model_copy(update={"source": "model"})
    # A rubric with any failed check cannot be an accept, whatever the verdict field says.
    if not (c.clauses_exist and c.claims_grounded and c.amount_follows and c.nothing_ignored):
        c = c.model_copy(update={"verdict": "revise"})
    cmd = _next(state, c)
    cmd.update["budget"] = budget_of(state).spend(tokens=tokens, steps=1)
    return cmd
