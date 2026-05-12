"""The eval runner against the graph, with a scripted 'perfect model' that follows the oracle.

A perfect model must score outcome_correct == 1 and unsafe_action == 0 on every slice for the
graph configs. If it does not, the plumbing (not the model) is wrong.
"""
import pytest

from app import llm
from app.risk_client import set_risk_client
from app.state import Intake, RiskVerdict
from backend_sim.factsheet import FactSheet, derive_risk
from evals.generate import build_records
from evals.run import Runner, score, summarize
from tests.conftest import FakeRisk
from tests.fakes import draft_ids, make_handler, reference_decision
from evals.oracle import decide

RECORDS = build_records(seed=7)
def _spread(slice_, k=8):
    rows = [x for x in RECORDS if x["slice"] == slice_]
    return rows[:: max(1, len(rows) // k)][:k]       # strided, so every variant in the slice is hit


SAMPLE = [r for s in ("clear", "partial", "escalate", "conflict", "missing", "adversarial") for r in _spread(s)]


async def run_config(config: str, records):
    runner = Runner(config)
    rows = []
    for rec in records:
        f = FactSheet.model_validate(rec["facts"])
        level, s = derive_risk(f)
        set_risk_client(FakeRisk(RiskVerdict(level=level, signals=[], score=s)))
        ref = decide(f)
        intake = Intake(claim_type=f.claim_type, order_id=f.order_id if f.mentions_order_id else None,
                        has_photo=False, summary="neutral summary")

        def drafter(messages, ref=ref):
            ev, cl = draft_ids(messages)
            return reference_decision(ref, ev, cl)

        llm.set_structured_handler(make_handler(intake, drafter))
        o = await runner.run_case({"facts": rec["facts"], "message": rec["message"],
                                   "customer_id": rec["customer_id"], "has_photo": rec["has_photo"]})
        rows.append({"id": rec["id"], "slice": rec["slice"], "rep": 0, "config": config, "outputs": o,
                     "scores": score(o, rec["reference"], judges=False)})
    return rows


@pytest.mark.parametrize("config", ["B", "C", "D", "E"])
async def test_perfect_model_scores_perfectly(isolated, config):
    rows = await run_config(config, SAMPLE)
    bad = [(r["id"], r["slice"], r["outputs"].get("error"), r["outputs"]["decision"], r["scores"]["outcome_correct"])
           for r in rows if not r["scores"]["outcome_correct"] or r["outputs"]["status"] == "failed"]
    assert not bad, bad
    s = summarize(rows)
    assert s["unsafe_actions"] == 0 and s["over_refunds"] == 0 and s["failed"] == 0
    assert s["outcome_acc"] == 1.0 and s["mean_amount_error"] == 0


async def test_perfect_model_cites_only_retrieved_clauses_and_walks_required_tools(isolated):
    rows = await run_config("E", SAMPLE)
    assert all(r["scores"]["clauses_valid"] for r in rows)
    assert all(r["scores"]["trajectory"] for r in rows), [r["id"] for r in rows if not r["scores"]["trajectory"]]
    assert summarize(rows)["retrieval_recall_at_6"] is not None


async def test_executed_only_when_no_human_needed(isolated):
    rows = await run_config("E", SAMPLE)
    by_id = {r["id"]: r for r in RECORDS}
    for r in rows:
        ref = by_id[r["id"]]["reference"]
        if r["outputs"]["executed"]:
            assert ref["outcome"].startswith("approve") and not ref["requires_human"]
            assert r["outputs"]["executed_amount_cents"] == ref["amount_cents"]
        elif ref["requires_human"]:
            assert r["outputs"]["status"] in ("escalated", "escalated_early")
