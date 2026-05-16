"""Risk service: signal tools, verdict normalisation, and the A2A contract against the real
ADK server (agent stubbed so no model is needed)."""
import json

import httpx
import pytest
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.agents import BaseAgent
from google.adk.events import Event
from google.genai import types

from backend_sim.db import Db, set_db
from backend_sim.factsheet import FactSheet, ItemFact, derive_risk
from backend_sim.seed import seed_fact_sheet
from risk_service import tools
from risk_service.agent import normalize, root_agent
from risk_service.contract import RiskVerdict as ServiceVerdict, level_for


@pytest.fixture
def db():
    d = Db("sqlite:///:memory:")
    d.init_schema()
    set_db(d)
    yield d
    set_db(None)


def sheet(**kw):
    base = dict(scenario_id="s", customer_id="c1", order_id="o1", items=[ItemFact(sku="A", price_cents=1000)])
    base.update(kw)
    return FactSheet(**base)


def test_signal_tools_read_seeded_facts(db):
    seed_fact_sheet(db, sheet(account_age_days=5, payment_changes_30d=2, address_reuse_accounts=3,
                              refunds_last_90d=2))
    assert tools.account_age("c1")["account_age_days"] == 5
    assert tools.payment_changes("c1")["payment_changes_30d"] == 2
    assert tools.address_reuse("c1")["address_reuse_accounts"] == 3
    assert tools.refund_velocity("c1")["refunds_last_90d"] == 2
    assert tools.account_age("nobody")["error"] == "not_found"


@pytest.mark.parametrize("kw", [{}, {"account_age_days": 5}, {"account_age_days": 5, "payment_changes_30d": 2},
                                {"address_reuse_accounts": 3, "refunds_last_90d": 2, "payment_changes_30d": 2}])
def test_scoring_rubric_matches_the_oracle(kw):
    """The scorer prompt's additive rubric and the oracle's derive_risk must agree."""
    f = sheet(**kw)
    level, score = derive_risk(f)
    assert level_for(score) == level


def test_normalize_derives_level_from_score_in_code():
    v = normalize({"score": 0.7, "level": "low", "signals": ["new_account"]})       # model said low; score says high
    assert (v.level, v.score) == ("high", 0.7)
    assert normalize(json.dumps({"score": 5, "signals": []})).score == 1.0
    with pytest.raises(KeyError):
        normalize({"level": "low"})


def test_agent_topology():
    assert [a.name for a in root_agent.sub_agents] == ["gather", "refine", "emit"]
    gather, refine, _ = root_agent.sub_agents
    assert {a.name for a in gather.sub_agents} == {"velocity", "account"}
    assert refine.max_iterations == 2


def test_contract_matches_main_service():
    from app.state import RiskVerdict as MainVerdict
    assert ServiceVerdict.model_json_schema()["properties"].keys() == MainVerdict.model_json_schema()["properties"].keys()
    assert MainVerdict.model_validate(ServiceVerdict(level="low", score=0.1).model_dump()).level == "low"


class StubRisk(BaseAgent):
    async def _run_async_impl(self, ctx):
        req = json.loads(ctx.user_content.parts[0].text)
        score = 0.7 if req["customer_id"] == "bad" else 0.0
        v = ServiceVerdict(level=level_for(score), signals=["stub"], score=score)
        yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part(text=v.model_dump_json())]))


async def test_a2a_roundtrip_and_agent_card():
    from app.risk_client import RiskClient
    app = to_a2a(StubRisk(name="risk_stub", description="stub"), host="risk", port=8001)
    async with app.router.lifespan_context(app):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://risk:8001")
        card = (await http.get("/.well-known/agent-card.json")).json()
        assert card["name"] == "risk_stub"
        client = RiskClient("http://risk:8001", httpx_client=http, retries=0, timeout_s=10)
        good = await client.assess("good", "o1", "damaged", headers={"x-trace": "1"})
        bad = await client.assess("bad", "o1", "damaged")
        assert good.level == "low" and bad.level == "high" and bad.score == 0.7


async def test_service_down_returns_none_and_opens_breaker():
    from app.risk_client import CircuitBreaker, RiskClient
    client = RiskClient("http://127.0.0.1:9", retries=1, timeout_s=2,
                        breaker=CircuitBreaker(2, 60))
    assert await client.assess("c", "o", "damaged") is None
    assert await client.assess("c", "o", "damaged") is None
    assert client.breaker.state == "open"
    assert await client.assess("c", "o", "damaged") is None        # short-circuited, no network attempt
