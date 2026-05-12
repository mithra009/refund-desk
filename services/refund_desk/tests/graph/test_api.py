import time

import pytest
from fastapi.testclient import TestClient

from app import api, llm
from backend_sim.factsheet import ItemFact
from backend_sim.seed import seed_fact_sheet
from tests.fakes import make_handler
from tests.graph.test_flow import facts, intake_for, oracle_drafter, refund_rows

H = {"X-Customer-Id": "c1"}


def wait_for(client, case_id, headers, want, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        body = client.get(f"/cases/{case_id}", headers=headers).json()
        if body["status"] in want:
            return body
        time.sleep(0.05)
    raise AssertionError(f"timed out, last status {body['status']}")


@pytest.fixture
def client(isolated, low_risk):
    with TestClient(api.app) as c:
        yield c


def script(f):
    llm.set_structured_handler(make_handler(intake_for(f), oracle_drafter(f)))


def test_open_case_runs_to_refund_and_hides_internals_from_customer(client, isolated):
    f = facts()
    seed_fact_sheet(isolated, f)
    script(f)
    r = client.post("/cases", json={"message": "arrived broken"}, headers=H)
    assert r.status_code == 202
    case_id = r.json()["case_id"]
    body = wait_for(client, case_id, H, {"refunded"})
    assert body["reply"] and body["refund_id"]
    assert "decision" not in body and "guard" not in body          # customer view has no internals
    assert len(refund_rows(isolated)) == 1


def test_auth_required_and_cases_are_private(client, isolated):
    assert client.post("/cases", json={"message": "x"}).status_code == 401
    f = facts()
    seed_fact_sheet(isolated, f)
    script(f)
    cid = client.post("/cases", json={"message": "x"}, headers=H).json()["case_id"]
    wait_for(client, cid, H, {"refunded"})
    assert client.get(f"/cases/{cid}", headers={"X-Customer-Id": "someone-else"}).status_code == 404


def test_review_flow_over_authority(client, isolated, monkeypatch):
    monkeypatch.setenv("REVIEWER_TOKEN", "placeholder-reviewer-token")
    R = {"X-Reviewer-Token": "placeholder-reviewer-token"}
    f = facts(items=[ItemFact(sku="CHAIR", price_cents=24900)])
    seed_fact_sheet(isolated, f)
    script(f)
    cid = client.post("/cases", json={"message": "x"}, headers=H).json()["case_id"]
    pending = wait_for(client, cid, R, {"pending_review"})
    assert pending["pending_review"]["reasons"] == ["over_authority"] and refund_rows(isolated) == []
    assert client.post(f"/cases/{cid}/review", json={"action": "approve"}).status_code == 403   # no token
    ok = client.post(f"/cases/{cid}/review", json={"action": "approve", "reviewer": "ann"}, headers=R)
    assert ok.status_code == 200 and ok.json()["refund_id"]
    again = client.post(f"/cases/{cid}/review", json={"action": "approve"}, headers=R)
    assert again.status_code == 409                                  # double click
    assert len(refund_rows(isolated)) == 1


def test_sse_stream_reports_nodes_then_terminal_event(client, isolated):
    f = facts()
    seed_fact_sheet(isolated, f)
    script(f)
    cid = client.post("/cases", json={"message": "x"}, headers=H).json()["case_id"]
    with client.stream("GET", f"/cases/{cid}/events", headers=H) as r:
        text = "".join(r.iter_text())
    assert "event: node" in text and "intake" in text and "event: done" in text


def test_health_and_ready(client):
    assert client.get("/healthz").json() == {"ok": True}
    body = client.get("/readyz").json()
    assert body["ready"] is True and body["postgres"] == "ok"
