import pytest

from app.guards import (cheap_checks, final_decision_after_review, guard_reasons, open_issues,
                        reply_violations, route_after_guard)
from app.state import Critique, Decision, PolicyClause, RiskVerdict

CL = [PolicyClause(id=i, title=i, text="") for i in ("R1", "R2", "R7")]
EV = [{"id": "ev_order_0", "source": "x", "question": "q", "finding": "f", "raw": {}, "confidence": 0.9}]
LOW = RiskVerdict(level="low", signals=[], score=0.0)


def dec(**kw):
    base = dict(outcome="approve_full", amount_cents=3000, clauses=["R2"], evidence_refs=["ev_order_0"],
                rationale="r", confidence=0.9)
    base.update(kw)
    return Decision(**base)


def reasons(d, risk=LOW, critiques=(), total=3000, evidence=EV):
    return guard_reasons(d, risk, list(critiques), total, evidence, authority_cents=10_000, conf_floor=0.6)


# ---- guard ---------------------------------------------------------------------
def test_clean_approval_has_no_reasons_and_executes():
    d = dec()
    assert reasons(d) == [] and route_after_guard(d, []) == "execute"


def test_deny_with_no_reasons_goes_to_reply():
    d = dec(outcome="deny", amount_cents=0)
    assert route_after_guard(d, reasons(d)) == "reply"


@pytest.mark.parametrize("cents,flag", [(10_000, False), (10_001, True)])
def test_authority_boundary_is_inclusive_at_100_dollars(cents, flag):
    d = dec(amount_cents=cents, outcome="approve_partial")
    assert ("over_authority" in reasons(d, total=20_000)) is flag


def test_model_escalation_always_goes_to_human():
    d = dec(outcome="escalate", amount_cents=0)
    assert route_after_guard(d, reasons(d)) == "human_review"


def test_risk_none_escalates():
    assert "risk" in reasons(dec(), risk=None)


def test_high_risk_escalates_medium_does_not():
    assert "risk" in reasons(dec(), risk=RiskVerdict(level="high", signals=[], score=0.7))
    assert "risk" not in reasons(dec(), risk=RiskVerdict(level="medium", signals=[], score=0.4))


def test_low_confidence_escalates():
    assert "low_confidence" in reasons(dec(confidence=0.59))
    assert "low_confidence" not in reasons(dec(confidence=0.6))


def test_unresolved_critique_escalates():
    assert "unresolved_critique" in reasons(dec(), critiques=[Critique(verdict="revise")])
    assert "unresolved_critique" not in reasons(dec(), critiques=[Critique(verdict="revise"), Critique(verdict="accept")])


def test_amount_over_order_total_escalates():
    assert "amount_exceeds_order" in reasons(dec(amount_cents=3001), total=3000)


def test_unknown_evidence_escalates():
    unknown = EV + [{"id": "ev_unknown_0", "source": "merge", "question": "q", "finding": "UNKNOWN",
                     "raw": {"unknown": True}, "confidence": 0.0}]
    assert "unknown_evidence" in reasons(dec(), evidence=unknown)


def test_multiple_reasons_accumulate():
    r = reasons(dec(amount_cents=20_000, confidence=0.1), risk=None, total=20_000)
    assert {"over_authority", "risk", "low_confidence"} <= set(r)


# ---- cheap checks --------------------------------------------------------------
def test_cheap_checks_pass_for_valid_draft():
    assert cheap_checks(dec(), CL, EV, 3000) == []


def test_cheap_checks_catch_each_defect():
    assert any("R99" in e for e in cheap_checks(dec(clauses=["R99"]), CL, EV, 3000))
    assert any("ev_nope" in e for e in cheap_checks(dec(evidence_refs=["ev_nope"]), CL, EV, 3000))
    assert any("exceeds" in e for e in cheap_checks(dec(amount_cents=5000), CL, EV, 3000))
    assert cheap_checks(dec(outcome="deny", amount_cents=100), CL, EV, 3000)
    assert cheap_checks(dec(outcome="approve_partial", amount_cents=0), CL, EV, 3000)
    assert any("approve_full" in e for e in cheap_checks(dec(outcome="approve_full", amount_cents=2000), CL, EV, 3000))
    assert cheap_checks(dec(clauses=[]), CL, EV, 3000)


def test_open_issues():
    assert not open_issues([]) and open_issues([Critique(verdict="revise")])


# ---- reply checks --------------------------------------------------------------
def test_reply_ok():
    assert reply_violations("We refunded $29.00 to your card.", 2900) == []


@pytest.mark.parametrize("text", ["Under R4 we refunded $29.00.", "Our fraud team reviewed it for $29.00.",
                                  "We have escalated this for $29.00."])
def test_reply_rejects_internal_terms(text):
    assert reply_violations(text, 2900)


def test_reply_rejects_wrong_amount_and_amount_without_refund():
    assert reply_violations("We refunded $30.00.", 2900)
    assert reply_violations("You paid $30.00 so we cannot help.", None)
    assert reply_violations("Sorry, we cannot refund this order.", None) == []


# ---- human review --------------------------------------------------------------
def test_review_actions():
    d = dec()
    assert final_decision_after_review(d, "approve", None, None, 3000) == d
    assert final_decision_after_review(d, "reject", None, None, 3000) is None
    e = final_decision_after_review(d, "edit", "approve_partial", 1000, 3000)
    assert (e.outcome, e.amount_cents) == ("approve_partial", 1000)


def test_edit_cannot_exceed_order_total_and_normalises_outcome():
    e = final_decision_after_review(dec(), "edit", "approve_partial", 99_999, 3000)
    assert (e.outcome, e.amount_cents) == ("approve_full", 3000)
    z = final_decision_after_review(dec(), "edit", "approve_full", 0, 3000)
    assert z.outcome == "deny" and z.amount_cents == 0


def test_approving_an_escalation_is_not_actionable():
    assert final_decision_after_review(dec(outcome="escalate", amount_cents=0), "approve", None, None, 3000) is None


def test_reviewer_can_decide_when_nothing_was_drafted():
    e = final_decision_after_review(None, "edit", "approve_partial", 1500, 3000)
    assert e.outcome == "approve_partial" and e.amount_cents == 1500
    assert final_decision_after_review(None, "approve", None, None, 3000) is None
