"""The smoke subset through the LangSmith pytest plugin (real models; opt in with RUN_LIVE=1).

    RUN_LIVE=1 LANGSMITH_TEST_SUITE=refund-desk-smoke pytest evals/tests/test_smoke_live.py

Each case runs 3 times; every run logs inputs, outputs, reference and feedback to LangSmith and
asserts the one hard rule: no unsafe action. Aggregate accuracy vs baseline is `python -m evals.smoke`.
"""
import asyncio
import os

import pytest

from evals import evaluators as ev
from evals import gate
from evals.run import Runner, load_records

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="RUN_LIVE=1 not set")]

CASES = gate.smoke_subset(load_records("dev")) if os.environ.get("RUN_LIVE") == "1" else []


@pytest.fixture(scope="module")
def runner():
    return Runner("E")


@pytest.mark.langsmith
@pytest.mark.parametrize("rep", range(3))
@pytest.mark.parametrize("rec", CASES, ids=[c["id"] for c in CASES])
def test_smoke_case(rec, rep, runner):
    from langsmith import testing as t
    inputs = {"facts": rec["facts"], "message": rec["message"], "customer_id": rec["customer_id"],
              "has_photo": rec["has_photo"]}
    t.log_inputs({"scenario_id": rec["id"], "message": rec["message"]})
    t.log_reference_outputs(rec["reference"])
    out = asyncio.run(runner.run_case(inputs))
    t.log_outputs({"decision": out["decision"], "status": out["status"]})
    for fn in (ev.outcome_correct, ev.unsafe_action, ev.clauses_valid):
        t.log_feedback(key=fn.__name__, score=float(fn(out, rec["reference"])))
    assert out["status"] != "failed", out.get("error")
    assert not ev.unsafe_action(out, rec["reference"])
