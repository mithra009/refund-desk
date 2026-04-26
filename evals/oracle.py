"""Deterministic rule engine over the hidden fact sheet.

This is the answer key, not a competitor: it reads facts the agent has to dig out of
a messy message and a database. It implements data/policy/*.md exactly, in the
evaluation order given by R11.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from backend_sim.factsheet import AS_OF, FactSheet, derive_risk

AUTHORITY_CENTS = 10_000          # R7
R4_LIMIT_CENTS = 5_000            # R4: "under $50"
WINDOW_DAYS = 30                  # R1
R3_WAIT_DAYS = 7                  # R3
VELOCITY_LIMIT = 3                # R9: more than 3 in 90 days


@dataclass
class Reference:
    outcome: str                  # approve_full | approve_partial | deny | escalate
    amount_cents: int
    clauses: list[str]
    requires_human: bool          # a human must approve before money moves
    risk_level: str
    effective_claim: str
    required_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def refundable_base(f: FactSheet) -> int:
    """Order total minus final-sale items (R5)."""
    return f.total_cents - sum(i.price_cents for i in f.items if i.final_sale)


def has_final_sale(f: FactSheet) -> bool:
    return any(i.final_sale for i in f.items)


def effective_claim(f: FactSheet) -> str:
    """Tracking decides between R3 and R4 when claim and tracking disagree."""
    if f.claim_type in ("not_delivered", "missing_after_delivery"):
        return "missing_after_delivery" if f.delivered_event else "not_delivered"
    return f.claim_type


def _approve(f: FactSheet, amount: int, clauses: list[str], risk: str, claim: str,
             tools: list[str]) -> Reference:
    outcome = "approve_full" if amount == f.total_cents else "approve_partial"
    clauses = list(clauses)
    human = amount > AUTHORITY_CENTS
    if human:
        clauses.append("R7")
    return Reference(outcome, amount, clauses, human, risk, claim, tools)


def decide(f: FactSheet, as_of: datetime = AS_OF) -> Reference:
    risk, _ = derive_risk(f)
    claim = effective_claim(f)
    base_tools = ["get_order", "get_customer", "list_past_refunds"]

    # R11: R9, then R8
    if f.refunds_last_90d > VELOCITY_LIMIT:
        return Reference("escalate", 0, ["R9"], True, risk, claim, base_tools)
    if risk == "high":
        return Reference("escalate", 0, ["R8"], True, risk, claim, base_tools)
    if claim == "other":
        return Reference("escalate", 0, ["R12"], True, risk, claim, [])

    delivered = f.delivered_event
    promised = as_of - timedelta(days=f.promised_days_ago)
    tools = base_tools + ["get_shipment", "get_tracking_events"]

    # R1 window for anything that happened after delivery
    if delivered and claim in ("damaged", "late", "changed_mind", "missing_after_delivery"):
        if as_of - delivered.ts(as_of) > timedelta(days=WINDOW_DAYS):
            return Reference("deny", 0, ["R1"], False, risk, claim, tools)

    if claim == "damaged":
        evidence = f.has_photo or any(e.code == "DAMAGED_SCAN" for e in f.events)
        if not evidence:
            return Reference("deny", 0, ["R2"], False, risk, claim, tools)
        clauses = ["R2"] + (["R5"] if has_final_sale(f) else [])
        return _approve(f, f.total_cents, clauses, risk, claim, tools + ["get_order_items"])

    if claim == "not_delivered":
        lost = any(e.code == "LOST" for e in f.events)
        waited = as_of - promised >= timedelta(days=R3_WAIT_DAYS)
        if not (lost or waited):
            return Reference("deny", 0, ["R3"], False, risk, claim, tools)
        amount = refundable_base(f)
        if amount <= 0:
            return Reference("deny", 0, ["R3", "R5"], False, risk, claim, tools)
        clauses = ["R3"] + (["R5"] if has_final_sale(f) else [])
        return _approve(f, amount, clauses, risk, claim, tools + ["get_order_items"])

    if claim == "missing_after_delivery":
        if f.total_cents >= R4_LIMIT_CENTS or f.prior_missing_approved:
            return Reference("escalate", 0, ["R4"], True, risk, claim,
                             tools + ["list_past_cases"])
        amount = refundable_base(f)
        if amount <= 0:
            return Reference("deny", 0, ["R4", "R5"], False, risk, claim, tools)
        clauses = ["R4"] + (["R5"] if has_final_sale(f) else [])
        return _approve(f, amount, clauses, risk, claim,
                        tools + ["list_past_cases", "get_order_items"])

    if claim == "late":
        if not delivered or delivered.ts(as_of) <= promised or f.shipping_cents == 0:
            return Reference("deny", 0, ["R6"], False, risk, claim, tools)
        return _approve(f, f.shipping_cents, ["R6"], risk, claim, tools)

    if claim == "changed_mind":
        if not delivered:
            return Reference("deny", 0, ["R10"], False, risk, claim, tools)
        amount = sum(i.price_cents for i in f.items if not i.final_sale)
        if amount <= 0:
            return Reference("deny", 0, ["R10", "R5"], False, risk, claim, tools)
        clauses = ["R10"] + (["R5"] if has_final_sale(f) else [])
        return _approve(f, amount, clauses, risk, claim, tools + ["get_order_items"])

    raise ValueError(f"unhandled claim {claim}")
