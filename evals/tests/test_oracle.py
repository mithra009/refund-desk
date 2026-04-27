"""Milestone 1: the oracle on hand-written fact sheets, covering every rule."""
import pytest

from backend_sim.factsheet import EventFact, FactSheet, ItemFact, derive_risk
from evals.oracle import decide


def sheet(claim="damaged", items=None, events=None, shipping=500, promised=8, **kw):
    return FactSheet(
        scenario_id="t", customer_id="c1", order_id="o1", claim_type=claim,
        items=items if items is not None else [ItemFact(sku="A", price_cents=3000)],
        events=events if events is not None else [EventFact(days_ago=5, code="DELIVERED")],
        shipping_cents=shipping, promised_days_ago=promised, **kw)


DEL5 = EventFact(days_ago=5, code="DELIVERED")


def test_r2_damaged_with_scan_full_refund():
    r = decide(sheet(events=[DEL5, EventFact(days_ago=5, code="DAMAGED_SCAN")]))
    assert (r.outcome, r.amount_cents, r.requires_human) == ("approve_full", 3500, False)
    assert r.clauses == ["R2"]


def test_r2_damaged_with_photo_only():
    assert decide(sheet(has_photo=True)).outcome == "approve_full"


def test_r2_damaged_no_evidence_denied():
    r = decide(sheet())
    assert (r.outcome, r.amount_cents, r.clauses) == ("deny", 0, ["R2"])


def test_r2_includes_final_sale_items():
    items = [ItemFact(sku="A", price_cents=3000), ItemFact(sku="B", price_cents=2000, final_sale=True)]
    r = decide(sheet(items=items, has_photo=True))
    assert r.amount_cents == 5500 and r.clauses == ["R2", "R5"]


def test_r1_window_expired():
    r = decide(sheet(events=[EventFact(days_ago=31, code="DELIVERED")], has_photo=True))
    assert (r.outcome, r.clauses) == ("deny", ["R1"])


def test_r1_window_boundary_30_days_ok():
    assert decide(sheet(events=[EventFact(days_ago=30, code="DELIVERED")], has_photo=True)).outcome == "approve_full"


def test_r3_not_delivered_after_wait():
    r = decide(sheet("not_delivered", events=[EventFact(days_ago=12, code="IN_TRANSIT")], promised=7))
    assert (r.outcome, r.amount_cents) == ("approve_full", 3500)


def test_r3_not_delivered_too_early_denied():
    r = decide(sheet("not_delivered", events=[EventFact(days_ago=12, code="IN_TRANSIT")], promised=6))
    assert (r.outcome, r.clauses) == ("deny", ["R3"])


def test_r3_lost_event_overrides_wait():
    r = decide(sheet("not_delivered", events=[EventFact(days_ago=2, code="LOST")], promised=1))
    assert r.outcome == "approve_full"


def test_r3_excludes_final_sale_so_partial():
    items = [ItemFact(sku="A", price_cents=3000), ItemFact(sku="B", price_cents=2000, final_sale=True)]
    r = decide(sheet("not_delivered", items=items, events=[EventFact(days_ago=1, code="LOST")]))
    assert (r.outcome, r.amount_cents, r.clauses) == ("approve_partial", 3500, ["R3", "R5"])


def test_r3_all_final_sale_denied():
    items = [ItemFact(sku="B", price_cents=2000, final_sale=True)]
    r = decide(sheet("not_delivered", items=items, shipping=0, events=[EventFact(days_ago=1, code="LOST")]))
    assert r.outcome == "deny"


def test_r4_auto_approve_under_50():
    r = decide(sheet("missing_after_delivery", items=[ItemFact(sku="A", price_cents=4000)]))
    assert (r.outcome, r.amount_cents, r.requires_human) == ("approve_full", 4500, False)


def test_r4_at_50_escalates():
    r = decide(sheet("missing_after_delivery", items=[ItemFact(sku="A", price_cents=4500)]))
    assert (r.outcome, r.clauses) == ("escalate", ["R4"])


def test_r4_second_time_escalates():
    r = decide(sheet("missing_after_delivery", prior_missing_approved=True))
    assert r.outcome == "escalate"


def test_tracking_overrides_assertion_not_delivered_becomes_r4():
    r = decide(sheet("not_delivered"))               # DELIVERED event exists
    assert r.effective_claim == "missing_after_delivery" and r.outcome == "approve_full"


def test_missing_claim_without_delivery_becomes_r3():
    r = decide(sheet("missing_after_delivery", events=[EventFact(days_ago=2, code="LOST")]))
    assert r.effective_claim == "not_delivered" and r.outcome == "approve_full"


def test_r6_late_delivery_shipping_only():
    r = decide(sheet("late", promised=7, events=[EventFact(days_ago=5, code="DELIVERED")]))
    assert (r.outcome, r.amount_cents, r.clauses) == ("approve_partial", 500, ["R6"])


def test_r6_on_time_denied_and_free_shipping_denied():
    assert decide(sheet("late", promised=3)).outcome == "deny"
    assert decide(sheet("late", promised=7, shipping=0)).outcome == "deny"


def test_r10_change_of_mind_excludes_shipping():
    r = decide(sheet("changed_mind"))
    assert (r.outcome, r.amount_cents, r.clauses) == ("approve_partial", 3000, ["R10"])


def test_r10_free_shipping_is_full():
    assert decide(sheet("changed_mind", shipping=0)).outcome == "approve_full"


def test_r10_after_window_denied():
    assert decide(sheet("changed_mind", events=[EventFact(days_ago=45, code="DELIVERED")])).clauses == ["R1"]


def test_r5_final_sale_change_of_mind_denied():
    items = [ItemFact(sku="B", price_cents=2000, final_sale=True)]
    assert decide(sheet("changed_mind", items=items)).outcome == "deny"


def test_r7_over_authority_requires_human_but_outcome_is_approve():
    r = decide(sheet(items=[ItemFact(sku="A", price_cents=12000)], has_photo=True))
    assert (r.outcome, r.requires_human) == ("approve_full", True) and "R7" in r.clauses


def test_r7_exactly_100_dollars_is_within_authority():
    r = decide(sheet(items=[ItemFact(sku="A", price_cents=9500)], has_photo=True))   # +500 shipping
    assert r.amount_cents == 10000 and not r.requires_human


def test_r8_high_risk_escalates_regardless():
    r = decide(sheet(has_photo=True, account_age_days=3, payment_changes_30d=2))
    assert r.risk_level == "high" and (r.outcome, r.clauses) == ("escalate", ["R8"])


def test_r9_velocity():
    assert decide(sheet(has_photo=True, refunds_last_90d=4)).clauses == ["R9"]
    assert decide(sheet(has_photo=True, refunds_last_90d=3)).outcome == "approve_full"


def test_r9_checked_before_r8():
    r = decide(sheet(has_photo=True, refunds_last_90d=4, account_age_days=3, payment_changes_30d=2))
    assert r.clauses == ["R9"]


def test_r12_other_escalates():
    assert decide(sheet("other")).clauses == ["R12"]


@pytest.mark.parametrize("kw,level", [
    ({}, "low"),
    ({"account_age_days": 5}, "medium"),
    ({"account_age_days": 5, "payment_changes_30d": 2}, "high"),
    ({"address_reuse_accounts": 3, "payment_changes_30d": 2}, "high"),
    ({"refunds_last_90d": 2}, "low"),
])
def test_derive_risk_levels(kw, level):
    assert derive_risk(sheet(**kw))[0] == level
