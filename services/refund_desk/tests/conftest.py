
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from app import llm, memory
from app.config import cfg
from app.graph import build_graph
from app.risk_client import RiskClient, set_risk_client
from app.state import Budget, RiskVerdict
from backend_sim.db import Db, set_db
from tests.fakes import ToolCallingFake


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    db = Db("sqlite:///:memory:")
    db.init_schema()
    set_db(db)
    memory.set_store(InMemoryStore())
    llm.set_structured_handler(None)
    for tier in ("fast", "fast_fallback", "strong", "strong_fallback", "critic"):
        llm.set_chat_model(tier, ToolCallingFake())
    monkeypatch.setattr(cfg, "checkpoint_dsn", None)      # a developer .env must not leak into tests
    monkeypatch.setattr(cfg, "critic_enabled", True)
    monkeypatch.setattr(cfg, "hierarchy_enabled", True)
    from app.retrieval import get_index
    get_index.cache_clear()
    yield db
    llm.set_structured_handler(None)
    set_risk_client(None)
    set_db(None)


class FakeRisk(RiskClient):
    """Stands in for the A2A call. level=None simulates the service being down."""

    def __init__(self, verdict: RiskVerdict | None):
        self.verdict = verdict

    async def assess(self, customer_id, order_id, claim_type, headers=None):
        return self.verdict


@pytest.fixture
def low_risk():
    set_risk_client(FakeRisk(RiskVerdict(level="low", signals=[], score=0.0)))


@pytest.fixture
def graph():
    return build_graph(checkpointer=InMemorySaver())


def case_input(customer_id: str, message: str = "my order arrived broken", case_id: str = "case-1",
               has_photo: bool = False) -> dict:
    return {"case_id": case_id, "customer_id": customer_id, "message": message,
            "has_photo": has_photo, "budget": Budget(), "evidence": [], "critiques": [],
            "revision_count": 0, "policy_hits": [], "plan": [], "spent": []}


def cfg_for(case_id: str) -> dict:
    return {"configurable": {"thread_id": case_id}, "recursion_limit": 60}
