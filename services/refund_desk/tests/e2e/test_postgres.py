"""Postgres-specific paths: schema, pgvector retrieval, AsyncPostgresSaver, restart + resume.

Needs a running Postgres 16 with pgvector, e.g. `docker compose up -d db`, and
    E2E_PG_URL=postgresql://postgres:<password>@localhost:5432/refund_desk
These tests were not run in the session that wrote them (no Docker daemon was available).
"""
import os

import pytest

PG = os.environ.get("E2E_PG_URL")
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not PG, reason="E2E_PG_URL not set")]


@pytest.fixture
def pg():
    from backend_sim.db import Db, set_db
    db = Db(PG)
    db.init_schema()
    db.reset()
    set_db(db)
    yield db
    set_db(None)


def test_schema_and_idempotency_constraint(pg):
    from backend_sim import payments
    from backend_sim.factsheet import FactSheet, ItemFact
    from backend_sim.seed import seed_fact_sheet
    f = FactSheet(scenario_id="p", customer_id="pc1", order_id="po1", items=[ItemFact(sku="A", price_cents=2000)])
    seed_fact_sheet(pg, f)
    a, b = payments.issue_refund("po1", 1000, "pg-k1"), payments.issue_refund("po1", 1000, "pg-k1")
    assert a[0] == b[0] and (a[1], b[1]) == (True, False)


def test_pgvector_dense_search_agrees_with_in_process(pg):
    from app.retrieval import PolicyIndex, build_query, index_to_db
    assert index_to_db(pg) == 25
    ix = PolicyIndex(db=pg)
    hits = [h.id for h in ix.search(build_query("damaged", ["carrier recorded DAMAGED_SCAN"]))]
    assert "R2" in hits[:3] and {"R7", "R8", "R9"} <= set(hits)


async def test_async_postgres_saver_interrupt_survives_a_new_saver(pg):
    """A fresh saver + graph (as after a restart) resumes the paused thread; one refund row."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.types import Command

    from app import llm
    from app.graph import build_graph
    from app.risk_client import set_risk_client
    from app.state import RiskVerdict
    from backend_sim.factsheet import ItemFact
    from backend_sim.seed import seed_fact_sheet
    from tests.conftest import FakeRisk, case_input, cfg_for
    from tests.fakes import ToolCallingFake, make_handler
    from tests.graph.test_flow import facts, intake_for, oracle_drafter

    f = facts(items=[ItemFact(sku="CHAIR", price_cents=24900)])
    seed_fact_sheet(pg, f)
    set_risk_client(FakeRisk(RiskVerdict(level="low", signals=[], score=0.0)))
    for tier in ("fast", "fast_fallback", "strong", "strong_fallback", "critic"):
        llm.set_chat_model(tier, ToolCallingFake())
    llm.set_structured_handler(make_handler(intake_for(f), oracle_drafter(f)))
    cfgd = cfg_for("pg-case-1")

    async with AsyncPostgresSaver.from_conn_string(PG) as saver:
        await saver.setup()
        out = await build_graph(checkpointer=saver).ainvoke(case_input("c1", case_id="pg-case-1"), cfgd)
        assert "__interrupt__" in out
    async with AsyncPostgresSaver.from_conn_string(PG) as saver2:          # "restart"
        g2 = build_graph(checkpointer=saver2)
        await g2.ainvoke(Command(resume={"action": "approve"}), cfgd)
        await g2.ainvoke(Command(resume={"action": "approve"}), cfgd)      # double click
    n = pg.one("SELECT COUNT(*) n FROM refunds WHERE idempotency_key = %s", ("pg-case-1:refund",))["n"]
    assert n == 1
