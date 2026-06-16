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


def test_normalize_accepts_whole_point_scorer_output():
    v = normalize({"score_pct": 70, "signals": ["new_account", "payment_changes"]})
    assert (v.level, v.score) == ("high", 0.7)
    assert normalize({"score_pct": 35, "signals": []}).level == "medium"
    assert normalize({"score_pct": 0, "signals": []}).level == "low"
    assert normalize({"score_pct": 250, "signals": []}).score == 1.0      # clamped


async def test_client_ignores_the_host_the_card_advertises():
    """The card says http://internal-name:8001; a client configured with another URL must use that."""
    from app.risk_client import RiskClient, pin_card_url
    app = to_a2a(StubRisk(name="risk_stub", description="stub"), host="internal-name", port=8001)
    async with app.router.lifespan_context(app):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8001")
        from a2a.client import A2ACardResolver
        card = await A2ACardResolver(http, "http://localhost:8001").get_agent_card()
        assert card.supported_interfaces[0].url.startswith("http://internal-name")
        assert pin_card_url(card, "http://localhost:8001/").supported_interfaces[0].url == "http://localhost:8001"
        client = RiskClient("http://localhost:8001", httpx_client=http, retries=0, timeout_s=10)
        assert (await client.assess("good", "o1", "damaged")).level == "low"


def test_missing_signals_fail_closed_instead_of_scoring_low():
    with pytest.raises(ValueError):
        normalize({"score_pct": 0, "signals": ["missing:account_age_days_lt_14"]})


async def _run_gather(request: dict) -> dict:
    """Run the real `gather` stage under an ADK Runner and return the session state."""
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from risk_service.agent import gather
    svc = InMemorySessionService()
    runner = Runner(agent=gather, app_name="t", session_service=svc, auto_create_session=True)
    msg = types.Content(role="user", parts=[types.Part(text=json.dumps(request))])
    async for _ in runner.run_async(user_id="u", session_id="s", new_message=msg):
        pass
    return (await svc.get_session(app_name="t", user_id="u", session_id="s")).state


async def test_signal_collectors_write_real_values_to_state(db):
    seed_fact_sheet(db, sheet(account_age_days=5, payment_changes_30d=2, address_reuse_accounts=3,
                              refunds_last_90d=2))
    state = await _run_gather({"customer_id": "c1", "order_id": "o1", "claim_type": "damaged"})
    assert "refunds_last_90d=2" in state["velocity_signals"] and "address_reuse_accounts=3" in state["velocity_signals"]
    assert "account_age_days=5" in state["account_signals"] and "payment_changes_30d=2" in state["account_signals"]
    assert "missing" not in state["velocity_signals"] + state["account_signals"]


async def test_unknown_customer_yields_missing_markers_not_zeros(db):
    state = await _run_gather({"customer_id": "ghost"})
    assert "missing:address_reuse" in state["velocity_signals"]
    assert "missing:account_age" in state["account_signals"]


async def test_garbage_request_is_marked_missing(db):
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from risk_service.agent import gather
    svc = InMemorySessionService()
    runner = Runner(agent=gather, app_name="t", session_service=svc, auto_create_session=True)
    async for _ in runner.run_async(user_id="u", session_id="s",
                                    new_message=types.Content(role="user", parts=[types.Part(text="not json")])):
        pass
    state = (await svc.get_session(app_name="t", user_id="u", session_id="s")).state
    assert "missing:" in state["velocity_signals"]


def test_agent_topology_uses_code_collectors_and_model_scorer():
    from risk_service.agent import SignalCollector
    gather, refine, _ = root_agent.sub_agents
    assert all(isinstance(a, SignalCollector) for a in gather.sub_agents)
    assert {a.name for a in refine.sub_agents} == {"scorer", "reviewer"}
