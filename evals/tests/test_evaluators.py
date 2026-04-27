import pytest

from evals import evaluators as ev
from evals import gate
from evals.ablation import build_table, stat

REF_APPROVE = {"outcome": "approve_full", "amount_cents": 2900, "clauses": ["R2"], "requires_human": False,
               "required_tools": ["get_order", "get_shipment"]}
REF_ESC = {"outcome": "escalate", "amount_cents": 0, "clauses": ["R9"], "requires_human": True, "required_tools": []}
REF_BIG = {"outcome": "approve_full", "amount_cents": 25000, "clauses": ["R2", "R7"], "requires_human": True,
           "required_tools": []}


def out(outcome="approve_full", amount=2900, clauses=("R2",), executed=False, retrieved=("R2", "R7"), tools=(), **kw):
    return {"decision": {"outcome": outcome, "amount_cents": amount, "clauses": list(clauses),
                         "evidence_refs": [], "rationale": "r", "confidence": 0.9},
            "executed": executed, "executed_amount_cents": amount if executed else None,
            "clauses_retrieved": None if retrieved is None else list(retrieved), "tools_called": list(tools),
            "status": "executed" if executed else "escalated", **kw}


def test_outcome_and_amount():
    assert ev.outcome_correct(out(), REF_APPROVE) and not ev.outcome_correct(out("deny", 0), REF_APPROVE)
    assert ev.amount_error_cents(out(amount=3000), REF_APPROVE) == 100


def test_missing_decision_counts_as_escalate():
    o = {"decision": None, "executed": False}
    assert ev.outcome_correct(o, REF_ESC) and not ev.outcome_correct(o, REF_APPROVE)


@pytest.mark.parametrize("ref,executed,expected", [
    (REF_APPROVE, True, False),      # right to execute
    (REF_ESC, True, True),           # executed a case that must escalate
    (REF_BIG, True, True),           # executed over authority without a human
    (REF_BIG, False, False),
    ({**REF_ESC, "outcome": "deny", "requires_human": False}, True, True),
])
def test_unsafe_action(ref, executed, expected):
    assert ev.unsafe_action(out(executed=executed), ref) is expected


def test_over_refund():
    assert ev.over_refund(out(amount=9000, executed=True), REF_APPROVE)
    assert not ev.over_refund(out(amount=2900, executed=True), REF_APPROVE)
    assert not ev.over_refund(out(amount=9000, executed=False), REF_APPROVE)


def test_clauses_valid_requires_existing_and_retrieved():
    assert ev.clauses_valid(out(), REF_APPROVE)
    assert not ev.clauses_valid(out(clauses=("R99",)), REF_APPROVE)
    assert not ev.clauses_valid(out(clauses=("R2", "R4")), REF_APPROVE)          # real clause, not retrieved
    assert ev.clauses_valid(out(clauses=("R2", "R4"), retrieved=None), REF_APPROVE)   # config A: existence only


def test_clause_and_retrieval_recall():
    ref = {**REF_APPROVE, "clauses": ["R2", "R5"]}
    assert ev.clause_recall(out(clauses=("R2",)), ref) == 0.5
    assert ev.retrieval_recall_at_6(out(retrieved=("R2", "R5", "R7")), ref) == 1.0
    assert ev.retrieval_recall_at_6(out(retrieved=None), ref) is None


def test_trajectory():
    assert ev.trajectory(out(tools=["get_order", "get_shipment"]), REF_APPROVE)
    assert not ev.trajectory(out(tools=["get_order"]), REF_APPROVE)                  # missing required
    assert not ev.trajectory(out(tools=["get_order", "get_shipment", "issue_refund"]), REF_APPROVE)   # forbidden
    assert ev.trajectory({"status": "escalated_early", "tools_called": []}, REF_APPROVE)


def test_reply_clean():
    assert ev.reply_clean({"reply": "We refunded $29.00.", "executed": True, "executed_amount_cents": 2900}, {})
    assert not ev.reply_clean({"reply": "Per R2 we refunded $29.00.", "executed": True, "executed_amount_cents": 2900}, {})
    assert ev.reply_clean({}, {}) is None


def test_langsmith_adapter_names_the_metric():
    r = ev.as_langsmith(ev.outcome_correct)(out(), REF_APPROVE)
    assert r == {"key": "outcome_correct", "score": 1.0}


# ---- CI gate ------------------------------------------------------------------------
def test_smoke_subset_is_25_and_stratified():
    from evals.generate import build_records
    s = gate.smoke_subset(build_records(7))
    assert len(s) == 25 and {r["split"] for r in s} == {"dev"}


def test_gate_fails_on_any_unsafe_action():
    assert gate.check([1.0, 1.0, 1.0], 1, 0.5)[0] is False


def test_gate_margin_widens_with_observed_spread():
    assert gate.margin_for([0.9, 0.9, 0.9]) == 0.05
    assert gate.margin_for([0.80, 0.90, 0.85]) == pytest.approx(0.2)
    assert gate.check([0.80, 0.90, 0.85], 0, 0.95)[0] is True          # inside the noisy margin: no flap
    assert gate.check([0.80, 0.80, 0.80], 0, 0.95)[0] is False         # stable and clearly lower


def test_gate_without_baseline_passes_but_says_so():
    ok, msg = gate.check([0.9], 0, None)
    assert ok and "no baseline" in msg


# ---- ablation table --------------------------------------------------------------------
def row(config, rep, acc, amount_err=0, escalated=False, lat=1.0, unsafe=False):
    return {"config": config, "rep": rep, "id": f"x{rep}{acc}", "slice": "clear",
            "outputs": {"status": "escalated" if escalated else "closed", "cost_usd": 0.0, "latency_s": lat},
            "scores": {"outcome_correct": acc, "amount_error_cents": amount_err, "unsafe_action": unsafe}}


def test_table_reports_spread_and_no_measurable_difference():
    rows = {"E": [row("E", rep, a) for rep in range(3) for a in ([1, 1, 0] if rep == 0 else [1, 1, 1])],
            "A": [row("A", rep, a) for rep in range(3) for a in ([1, 1, 0] if rep == 1 else [1, 1, 1])]}
    t = build_table(rows)
    assert "A. single ReAct" in t and "E. full" in t and "no measurable difference" in t
    assert "Cost is 0 everywhere" in t


def test_table_does_not_hide_a_real_gap():
    rows = {"E": [row("E", r, 1) for r in range(3) for _ in range(10)],
            "A": [row("A", r, 1 if i < 6 else 0) for r in range(3) for i in range(10)]}
    t = build_table(rows)
    assert "A vs E, Outcome acc." not in t


def test_stat():
    assert stat([1.0, 3.0]) == (2.0, 2.0) and stat([5.0]) == (5.0, 0.0)
