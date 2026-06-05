"""Pure rule checks. No model, no I/O. Heavily unit-tested.

Everything that must never be broken lives here rather than in a prompt.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable

from app.state import Critique, Decision, Evidence, PolicyClause, RiskVerdict

# R7, R8, R9: authority clauses that apply to every case, regardless of retrieval score.
ALWAYS_CLAUSES = ("R7", "R8", "R9")


# ---- cheap draft validation (runs before the critic, costs no tokens) ----------
def cheap_checks(d: Decision, clauses: Iterable[PolicyClause], evidence: Iterable[Evidence],
                 order_total_cents: int | None) -> list[str]:
    errors: list[str] = []
    known_clauses = {c.id for c in clauses}
    known_evidence = {e["id"] for e in evidence}
    for c in d.clauses:
        if c not in known_clauses:
            errors.append(f"clause {c} was not retrieved or does not exist")
    for e in d.evidence_refs:
        if e not in known_evidence:
            errors.append(f"evidence id {e} does not exist")
    if order_total_cents is not None and d.amount_cents > order_total_cents:
        errors.append(f"amount {d.amount_cents} exceeds order total {order_total_cents}")
    if d.outcome in ("deny", "escalate") and d.amount_cents != 0:
        errors.append(f"outcome {d.outcome} must have amount 0")
    if d.outcome in ("approve_full", "approve_partial") and d.amount_cents <= 0:
        errors.append("an approval needs a positive amount")
    if d.outcome == "approve_full" and order_total_cents is not None and d.amount_cents != order_total_cents:
        errors.append("approve_full must equal the order total; use approve_partial otherwise")
    if not d.clauses:
        errors.append("no clause cited")
    return errors


def open_issues(critiques: list[Critique]) -> bool:
    """True if the last critique still asks for a revision (cap reached with problems open)."""
    return bool(critiques) and critiques[-1].verdict == "revise"


# ---- R1: the 30-day return window, checked from the database, not from the model ------
WINDOW_DAYS = 30
WINDOW_CLAIMS = ("damaged", "late", "changed_mind", "missing_after_delivery")


def outside_return_window(claim_type: str | None, delivered_at: datetime | None, now: datetime,
                          days: int = WINDOW_DAYS) -> bool:
    """True if the claim is bound by R1 and delivery was more than `days` ago."""
    if claim_type not in WINDOW_CLAIMS or delivered_at is None:
        return False
    return now - delivered_at > timedelta(days=days)


# ---- the guard -----------------------------------------------------------------
def guard_reasons(d: Decision, risk: RiskVerdict | None, critiques: list[Critique],
                  order_total_cents: int, evidence: list[Evidence],
                  authority_cents: int, conf_floor: float, outside_window: bool = False) -> list[str]:
    reasons: list[str] = []
    if d.outcome == "escalate":
        reasons.append("model_escalated")
    if d.amount_cents > authority_cents:
        reasons.append("over_authority")
    if risk is None or risk.level == "high":      # risk service down => more cautious, not less
        reasons.append("risk")
    if d.confidence < conf_floor:
        reasons.append("low_confidence")
    if open_issues(critiques):
        reasons.append("unresolved_critique")
    if d.amount_cents > order_total_cents:
        reasons.append("amount_exceeds_order")
    if outside_window and d.outcome.startswith("approve"):
        reasons.append("outside_return_window")
    if any(e["raw"].get("unknown") for e in evidence):
        reasons.append("unknown_evidence")
    return reasons


def route_after_guard(d: Decision, reasons: list[str]) -> str:
    if reasons:
        return "human_review"
    return "execute" if d.outcome.startswith("approve") else "reply"


# ---- customer reply checks -----------------------------------------------------
_CLAUSE_ID = re.compile(r"\bR\d{1,2}\b")
_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
_INTERNAL = re.compile(r"\b(risk|fraud|suspicio\w*|escalat\w*|clause|guard|critic|confidence|audit)\b", re.I)


def reply_violations(reply: str, refunded_cents: int | None) -> list[str]:
    problems: list[str] = []
    if _CLAUSE_ID.search(reply):
        problems.append("mentions an internal clause id")
    if _INTERNAL.search(reply):
        problems.append("mentions internal process or risk terms")
    amounts = [round(float(m.replace(",", "")) * 100) for m in _MONEY.findall(reply)]
    if refunded_cents is None:
        if amounts:
            problems.append("quotes an amount although no refund was issued")
    else:
        for a in amounts:
            if a != refunded_cents:
                problems.append(f"quotes {a} cents but the refund is {refunded_cents}")
    return problems


def final_decision_after_review(d: Decision | None, action: str, outcome: str | None,
                                amount_cents: int | None, order_total_cents: int) -> Decision | None:
    """Apply a human review to a draft. Returns the decision to act on, or None for reject."""
    if action == "reject":
        return None
    if d is None:                         # nothing was drafted (unclassifiable, budget): the reviewer decides
        if action == "edit" and outcome:
            amount = max(0, min(amount_cents or 0, order_total_cents))
            if outcome.startswith("approve") and amount == 0:
                outcome = "deny"
            if outcome in ("deny", "escalate"):
                amount = 0
            return Decision(outcome=outcome, amount_cents=amount, clauses=[], evidence_refs=[],
                            rationale="decided by human reviewer", confidence=1.0)
        return None
    if action == "approve":
        if d.outcome == "escalate":      # approving an escalation without saying what to do is not actionable
            return None
        return d
    new_outcome = outcome or d.outcome
    new_amount = d.amount_cents if amount_cents is None else amount_cents
    if new_outcome in ("deny", "escalate"):
        new_amount = 0
    new_amount = max(0, min(new_amount, order_total_cents))
    if new_outcome.startswith("approve"):
        if new_amount == 0:
            new_outcome = "deny"
        else:
            new_outcome = "approve_full" if new_amount == order_total_cents else "approve_partial"
    return d.model_copy(update={"outcome": new_outcome, "amount_cents": new_amount})
