"""Child process for the durability test. Usage: durability_child.py <pause|resume> <workdir>

pause : seed an over-authority case, run to the human-review interrupt, then die abruptly.
resume: a brand-new process resumes the same thread from the checkpoint file and approves.
Models are scripted fakes; the database and checkpoints are files shared between processes.
"""
import asyncio
import os
import sys

workdir, mode = sys.argv[2], sys.argv[1]
os.environ["DATABASE_URL"] = f"sqlite:///{workdir}/app.db"

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver   # noqa: E402
from langgraph.types import Command                            # noqa: E402

from app import llm                                            # noqa: E402
from app.graph import build_graph                              # noqa: E402
from app.risk_client import set_risk_client                    # noqa: E402
from app.state import RiskVerdict                              # noqa: E402
from backend_sim.db import get_db                              # noqa: E402
from backend_sim.factsheet import ItemFact                     # noqa: E402
from backend_sim.seed import seed_fact_sheet                   # noqa: E402
from tests.conftest import FakeRisk, case_input, cfg_for      # noqa: E402
from tests.fakes import ToolCallingFake, make_handler          # noqa: E402
from tests.graph.test_flow import facts, intake_for, oracle_drafter   # noqa: E402

F = facts(items=[ItemFact(sku="CHAIR", price_cents=24900)])
set_risk_client(FakeRisk(RiskVerdict(level="low", signals=[], score=0.0)))
for tier in ("fast", "fast_fallback", "strong", "strong_fallback", "critic"):
    llm.set_chat_model(tier, ToolCallingFake())
llm.set_structured_handler(make_handler(intake_for(F), oracle_drafter(F)))


async def main():
    db = get_db()
    async with AsyncSqliteSaver.from_conn_string(f"{workdir}/ckpt.db") as saver:
        graph = build_graph(checkpointer=saver)
        if mode == "pause":
            seed_fact_sheet(db, F)
            out = await graph.ainvoke(case_input("c1"), cfg_for("case-1"))
            assert "__interrupt__" in out
            n = db.one("SELECT COUNT(*) n FROM refunds WHERE idempotency_key LIKE 'case-%'")["n"]
            print(f"PAUSED refunds={n}", flush=True)
            os._exit(9)                         # no cleanup, no graceful shutdown
        out = await graph.ainvoke(Command(resume={"action": "approve", "reviewer": "ann"}), cfg_for("case-1"))
        n = db.one("SELECT COUNT(*) n FROM refunds WHERE idempotency_key LIKE 'case-%'")["n"]
        print(f"RESUMED refunds={n} refund_id={out.get('refund_id')}", flush=True)


asyncio.run(main())
