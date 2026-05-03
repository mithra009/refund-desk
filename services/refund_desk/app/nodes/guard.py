"""Guard: pure code, no model. Authority limits and arithmetic cannot be talked past."""
from __future__ import annotations

from typing import Literal

from langgraph.types import Command

from app import audit
from app.config import cfg
from app.guards import guard_reasons, route_after_guard
from app.nodes.common import order_total, usable_evidence
from app.state import CaseState, GuardResult


def guard(state: CaseState) -> Command[Literal["execute", "human_review", "reply"]]:
    d = state["draft"]
    total = order_total(state)
    reasons = guard_reasons(
        d, state.get("risk"), state.get("critiques", []),
        order_total_cents=total if total is not None else -1,    # unknown order: any amount exceeds it
        evidence=usable_evidence(state.get("evidence", [])),
        authority_cents=cfg.authority_cents, conf_floor=cfg.conf_floor)
    goto = route_after_guard(d, reasons)
    audit.write(state["case_id"], "guard", {"reasons": reasons, "route": goto})
    update: dict = {"guard": GuardResult(reasons=reasons)}
    if goto in ("execute", "reply"):
        update["final"] = d
    return Command(goto=goto, update=update)
