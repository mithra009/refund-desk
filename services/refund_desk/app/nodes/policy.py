"""Policy retrieval: a plain function with a retriever. No agent."""
from __future__ import annotations

from app import audit
from app.nodes.common import usable_evidence
from app.retrieval import build_query, get_index
from app.state import CaseState


def policy(state: CaseState) -> dict:
    findings = [e["finding"] for e in usable_evidence(state.get("evidence", []))
                if not e["raw"].get("unknown")]
    query = build_query(state["intake"].claim_type, findings)    # never the raw customer message
    hits = get_index().search(query)
    audit.write(state["case_id"], "policy_retrieved", {"clauses": [h.id for h in hits]})
    return {"policy_hits": hits}
