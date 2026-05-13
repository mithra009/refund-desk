"""Ablation configs A (single ReAct) and B (pipeline) run end to end with fakes."""
from langgraph.checkpoint.memory import InMemorySaver

from app import llm
from app.graph import build_graph
from app.variants import run_baseline
from backend_sim.seed import seed_fact_sheet
from tests.conftest import case_input, cfg_for
from tests.fakes import make_handler
from tests.graph.test_flow import facts, intake_for, oracle_drafter, refund_rows


async def test_pipeline_variant_has_no_workers_and_no_critic(isolated, low_risk):
    f = facts()
    seed_fact_sheet(isolated, f)
    g = build_graph(checkpointer=InMemorySaver(), variant="pipeline")
    assert not {"order_worker", "critic", "evidence_merge"} & set(g.get_graph().nodes)
    log = []
    llm.set_structured_handler(make_handler(intake_for(f), oracle_drafter(f), log=log))
    out = await g.ainvoke(case_input("c1"), cfg_for("case-1"))
    assert out["refund_id"] and len(refund_rows(isolated)) == 1
    assert not any(n in ("Plan", "Critique", "EvidenceOut") for n, _ in log)


async def test_pipeline_guard_still_blocks_over_authority(isolated, low_risk):
    from backend_sim.factsheet import ItemFact
    f = facts(items=[ItemFact(sku="CHAIR", price_cents=24900)])
    seed_fact_sheet(isolated, f)
    g = build_graph(checkpointer=InMemorySaver(), variant="pipeline")
    llm.set_structured_handler(make_handler(intake_for(f), oracle_drafter(f)))
    out = await g.ainvoke(case_input("c1"), cfg_for("case-1"))
    assert "__interrupt__" in out and refund_rows(isolated) == []


def test_baseline_acts_on_its_own_decision_without_guard(isolated):
    """Config A has no guard: an over-authority approval executes. That is the point of the ablation."""
    from backend_sim.factsheet import ItemFact
    f = facts(items=[ItemFact(sku="CHAIR", price_cents=24900)])
    seed_fact_sheet(isolated, f)
    llm.set_structured_handler(make_handler(intake_for(f), oracle_drafter(f)))
    out = run_baseline("case-a", "c1", "Order id: o1 arrived broken")
    assert out["decision"].outcome == "approve_full" and out["executed"]
    assert isolated.one("SELECT amount_cents FROM refunds WHERE idempotency_key = 'case-a:refund'")["amount_cents"] == 25400
    assert {r["tool"] for r in out["tool_results"]} >= {"get_order", "get_shipment"}
