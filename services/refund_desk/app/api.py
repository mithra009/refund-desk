"""FastAPI: open cases, stream progress, review, health.

Auth is simulated. If REFUND_DESK_AUTH_TOKENS (JSON {token: customer_id}) is set, customers
authenticate with `Authorization: Bearer <token>`; otherwise, in dev only, the X-Customer-Id
header is trusted. The customer id always comes from here, never from the message.
Reviewer endpoints require X-Reviewer-Token when REVIEWER_TOKEN is set.
"""
from __future__ import annotations

import asyncio
import sys
import json
import logging
import os
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app import audit, feedback, memory
from app.config import cfg
from app.graph import build_graph
from app.observability import run_tags
from app.retrieval import index_to_db
from app.state import Budget, HumanReview
from backend_sim.db import get_db


if sys.platform == "win32":      # psycopg async (the Postgres checkpointer) cannot run on the Proactor loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


log = logging.getLogger("refund_desk")


class OpenCase(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    has_photo: bool = False


class Runtime:
    graph = None
    tasks: set = set()
    events: dict[str, list[dict]] = {}
    waiters: dict[str, asyncio.Event] = {}


rt = Runtime()


def _config(case_id: str) -> dict:
    return {"configurable": {"thread_id": case_id}, "recursion_limit": cfg.recursion_limit, **run_tags(case_id)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        db = get_db()
        if not db.is_sqlite:
            index_to_db(db)
        checkpointer, store = InMemorySaver(), None
        if cfg.checkpoint_dsn:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from langgraph.store.postgres import AsyncPostgresStore
            checkpointer = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(cfg.checkpoint_dsn))
            await checkpointer.setup()
            store = await stack.enter_async_context(AsyncPostgresStore.from_conn_string(cfg.checkpoint_dsn))
            await store.setup()
            memory.set_store(store)
        rt.graph = build_graph(checkpointer=checkpointer, store=store)
        yield


app = FastAPI(title="Refund Desk", lifespan=lifespan)


# ---- auth -----------------------------------------------------------------------
def current_customer(authorization: str | None = Header(None), x_customer_id: str | None = Header(None)) -> str:
    tokens = os.environ.get("REFUND_DESK_AUTH_TOKENS")
    if tokens:
        token = (authorization or "").removeprefix("Bearer ").strip()
        customer = json.loads(tokens).get(token)
        if not customer:
            raise HTTPException(401, "invalid token")
        return customer
    if not x_customer_id:
        raise HTTPException(401, "X-Customer-Id required (dev auth)")
    return x_customer_id


def is_reviewer(x_reviewer_token: str | None = Header(None)) -> bool:
    expected = os.environ.get("REVIEWER_TOKEN")
    return bool(expected) and x_reviewer_token == expected


def require_reviewer(ok: bool = Depends(is_reviewer)) -> None:
    if not ok and os.environ.get("REVIEWER_TOKEN"):
        raise HTTPException(403, "reviewer token required")


# ---- running a case ---------------------------------------------------------------
def _emit(case_id: str, event: dict) -> None:
    rt.events.setdefault(case_id, []).append(event)
    w = rt.waiters.get(case_id)
    if w:
        w.set()


async def _drive(case_id: str, payload) -> None:
    """Run (or resume) the graph, publishing node-level progress for SSE."""
    try:
        async for chunk in rt.graph.astream(payload, _config(case_id), stream_mode="updates"):
            for node in chunk:
                _emit(case_id, {"event": "interrupt" if node == "__interrupt__" else "node", "node": node})
        snap = await rt.graph.aget_state(_config(case_id))
        status = "pending_review" if snap.next else "done"
        get_db().execute("UPDATE cases SET status = %s WHERE id = %s AND status IN ('open','pending_review')",
                         (status, case_id))
        _emit(case_id, {"event": status})
    except Exception as e:  # surfaced to the client and the audit log, not swallowed
        log.exception("case %s failed", case_id)
        audit.write(case_id, "run_failed", {"error": type(e).__name__, "detail": str(e)[:500]})
        get_db().execute("UPDATE cases SET status = 'failed' WHERE id = %s", (case_id,))
        _emit(case_id, {"event": "failed", "error": type(e).__name__})


def _own_case(case_id: str, customer_id: str | None, reviewer: bool = False) -> dict:
    row = get_db().one("SELECT * FROM cases WHERE id = %s", (case_id,))
    if row is None or (not reviewer and row["customer_id"] != customer_id):
        raise HTTPException(404, "case not found")
    return row


@app.post("/cases", status_code=202)
async def open_case(body: OpenCase, bg: BackgroundTasks, customer_id: str = Depends(current_customer)):
    case_id = f"case-{uuid.uuid4().hex[:12]}"
    db = get_db()
    db.execute("INSERT INTO cases (id, thread_id, customer_id, status, created_at) VALUES (%s,%s,%s,%s,%s)",
               (case_id, case_id, customer_id, "open", db.ts(datetime.now(timezone.utc))))
    audit.write(case_id, "opened", {"has_photo": body.has_photo}, actor=customer_id)
    payload = {"case_id": case_id, "customer_id": customer_id, "message": body.message,
               "has_photo": body.has_photo, "budget": Budget(), "evidence": [], "critiques": [],
               "revision_count": 0, "policy_hits": [], "plan": [], "spent": []}
    rt.events[case_id] = []
    task = asyncio.create_task(_drive(case_id, payload))
    rt.tasks.add(task)
    task.add_done_callback(rt.tasks.discard)
    return {"case_id": case_id, "status": "open"}


async def _view(case_id: str, reviewer: bool) -> dict:
    row = get_db().one("SELECT * FROM cases WHERE id = %s", (case_id,))
    snap = await rt.graph.aget_state(_config(case_id))
    v = snap.values or {}
    pending = None
    for t in snap.tasks:
        if t.interrupts:
            pending = t.interrupts[0].value
    out = {"case_id": case_id, "status": row["status"], "reply": v.get("reply"),
           "refund_id": v.get("refund_id")}
    if reviewer:      # internals are for staff only
        final = v.get("final") or v.get("draft")
        out.update({"decision": final.model_dump() if final else None, "pending_review": pending,
                    "guard": v["guard"].model_dump() if v.get("guard") else None})
    return out


@app.get("/cases/{case_id}")
async def get_case(case_id: str, customer_id: str | None = Header(None, alias="X-Customer-Id"),
                   authorization: str | None = Header(None), reviewer: bool = Depends(is_reviewer)):
    if reviewer:
        _own_case(case_id, None, reviewer=True)
    else:
        _own_case(case_id, current_customer(authorization, customer_id))
    return await _view(case_id, reviewer)


@app.get("/cases/{case_id}/events")
async def case_events(case_id: str, customer_id: str | None = Header(None, alias="X-Customer-Id"),
                      authorization: str | None = Header(None)):
    _own_case(case_id, current_customer(authorization, customer_id))

    async def stream():
        sent = 0
        while True:
            events = rt.events.get(case_id, [])
            while sent < len(events):
                ev = events[sent]
                sent += 1
                yield {"event": ev["event"], "data": json.dumps(ev)}
                if ev["event"] in ("done", "pending_review", "failed"):
                    return
            waiter = rt.waiters.setdefault(case_id, asyncio.Event())
            waiter.clear()
            if sent >= len(rt.events.get(case_id, [])):
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
    return EventSourceResponse(stream())


@app.post("/cases/{case_id}/review", dependencies=[Depends(require_reviewer)])
async def review_case(case_id: str, review: HumanReview):
    _own_case(case_id, None, reviewer=True)
    snap = await rt.graph.aget_state(_config(case_id))
    if not snap.next:
        raise HTTPException(409, "case is not awaiting review")      # e.g. a double click after completion
    draft = (snap.values or {}).get("draft")
    if review.action == "approve" and (draft is None or draft.outcome == "escalate"):
        raise HTTPException(422, "nothing to approve: edit with an explicit outcome, or reject")
    if review.action == "edit" and draft is None and not review.outcome:
        raise HTTPException(422, "edit needs an outcome when nothing was drafted")
    await _drive(case_id, Command(resume=review.model_dump()))
    snap = await rt.graph.aget_state(_config(case_id))
    v = snap.values or {}
    final = v.get("final")
    feedback.record_review(case_id, v["intake"].summary if v.get("intake") else None,
                           draft.model_dump() if draft else None, review.model_dump(),
                           final.model_dump() if final else None)
    return await _view(case_id, reviewer=True)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    checks = {}
    try:
        get_db().one("SELECT 1 AS ok")
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"down: {type(e).__name__}"
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            r = await c.get(f"{cfg.risk_service_url}/.well-known/agent-card.json")
            checks["risk_service"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as e:
        checks["risk_service"] = f"down: {type(e).__name__}"
    ready = checks["postgres"] == "ok"            # risk down degrades (escalates), it does not stop intake
    if not ready:
        raise HTTPException(503, checks)
    return {"ready": True, **checks}
