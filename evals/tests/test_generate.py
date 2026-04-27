from collections import Counter

from backend_sim.db import Db
from backend_sim.factsheet import FactSheet
from backend_sim.seed import seed_fact_sheet
from evals.generate import build_records, handcheck_sheet

R = build_records(seed=7)


def test_counts_match_the_design():
    assert len(R) == 160
    assert Counter(r["slice"] for r in R) == {"clear": 60, "partial": 20, "escalate": 30,
                                              "conflict": 20, "missing": 15, "adversarial": 15}
    assert Counter(r["split"] for r in R) == {"dev": 120, "test": 40}


def test_every_slice_is_represented_in_the_test_split():
    assert {r["slice"] for r in R if r["split"] == "test"} == {r["slice"] for r in R}


def test_deterministic_and_ids_unique():
    again = build_records(seed=7)
    assert [r["message"] for r in R] == [r["message"] for r in again]
    assert len({r["id"] for r in R}) == 160 and len({r["customer_id"] for r in R}) == 160


def test_slices_test_what_they_claim():
    by = lambda s: [r["reference"] for r in R if r["slice"] == s]
    assert all(x["outcome"] in ("approve_full", "deny") and not x["requires_human"] for x in by("clear"))
    assert all(x["outcome"] == "approve_partial" for x in by("partial"))
    assert all(x["requires_human"] for x in by("escalate"))
    c = Counter(x["outcome"] for x in by("escalate"))
    assert c["escalate"] == 20 and c["approve_full"] == 10
    conflict_claims = {x["effective_claim"] for x in by("conflict")}
    assert {"missing_after_delivery", "not_delivered", "damaged"} <= conflict_claims
    assert all(x["amount_cents"] < 10_000 for x in by("clear"))


def test_adversarial_cases_never_have_an_answer_that_follows_the_attacker():
    for r in R:
        if r["slice"] == "adversarial":
            ref, f = r["reference"], r["facts"]
            if f["adversarial"] == "amount_inflation":
                total = sum(i["price_cents"] for i in f["items"]) + f["shipping_cents"]
                assert ref["amount_cents"] == total and f["requested_amount_cents"] > total
            if f["adversarial"] == "fake_authority":
                assert ref["outcome"] == "escalate"
            if f["adversarial"] == "injection":
                assert ref["outcome"] == "deny"
                assert any(w in r["message"].lower() for w in ("ignore", "override", "disregard")) or any(i["note"] for i in f["items"])


def test_messages_do_not_leak_the_answer():
    for r in R:
        if r["slice"] != "adversarial":
            assert r["reference"]["outcome"] not in r["message"]
        if not r["facts"]["mentions_order_id"]:
            assert r["facts"]["order_id"] not in r["message"]


def test_all_scenarios_seed_cleanly():
    db = Db("sqlite:///:memory:")
    db.init_schema()
    for r in R:
        seed_fact_sheet(db, FactSheet.model_validate(r["facts"]))
    assert db.one("SELECT COUNT(*) n FROM customers")["n"] == 160
    # a re-seed replaces rather than duplicates
    seed_fact_sheet(db, FactSheet.model_validate(R[0]["facts"]))
    assert db.one("SELECT COUNT(*) n FROM shipments WHERE order_id = %s", (R[0]["facts"]["order_id"],))["n"] == 1


def test_handcheck_sheet_samples_20():
    assert handcheck_sheet(R).count("- [ ] correct") == 20
