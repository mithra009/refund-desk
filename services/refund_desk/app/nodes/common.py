"""Helpers shared by nodes."""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import RetryPolicy

from app.state import Budget, CaseState, Evidence
from backend_sim.db import get_db

# Retry model and network calls with backoff. Never used on `execute`: idempotency covers it.
MODEL_RETRY = RetryPolicy(max_attempts=3, initial_interval=1.0, backoff_factor=2.0, jitter=True,
                          retry_on=Exception)


def msgs(system: str, human: str) -> list:
    return [SystemMessage(content=system), HumanMessage(content=human)]


def budget_of(state: CaseState) -> Budget:
    return state.get("budget") or Budget()


def order_row(state: CaseState) -> dict | None:
    """The case's order, from the database, only if it belongs to the case's customer."""
    intake = state.get("intake")
    if intake is None or not intake.order_id:
        return None
    return get_db().one("SELECT * FROM orders WHERE id = %s AND customer_id = %s",
                        (intake.order_id, state["customer_id"]))


def order_total(state: CaseState) -> int | None:
    o = order_row(state)
    return None if o is None else int(o["total_cents"])


def usable_evidence(evidence: list[Evidence], floor: float | None = None) -> list[Evidence]:
    """Deduped by id, low-confidence findings dropped. Unknown placeholders are kept so the
    decision and the guard can see that something is missing."""
    from app.config import cfg
    floor = cfg.evidence_conf_floor if floor is None else floor
    seen: dict[str, Evidence] = {}
    for e in evidence:
        if e["id"] in seen and seen[e["id"]]["confidence"] >= e["confidence"]:
            continue
        seen[e["id"]] = e
    return [e for e in seen.values() if e["confidence"] >= floor or e["raw"].get("unknown")]
