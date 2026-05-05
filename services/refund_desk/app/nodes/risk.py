"""risk_check: the A2A call to the ADK risk service. Failure yields risk=None (guard escalates)."""
from __future__ import annotations

from app.risk_client import get_risk_client, trace_headers


async def risk_check(inp: dict) -> dict:
    verdict = await get_risk_client().assess(inp["customer_id"], inp.get("order_id"),
                                             inp.get("claim_type"), headers=trace_headers())
    if verdict is None:
        return {"risk": None, "spent": [{}]}
    ev = {"id": "ev_risk_0", "source": "risk_service", "question": "risk assessment",
          "finding": f"Risk level {verdict.level} (score {verdict.score}); signals: "
                     f"{', '.join(verdict.signals) or 'none'}.",
          "raw": verdict.model_dump(), "confidence": 1.0}
    return {"risk": verdict, "evidence": [ev], "spent": [{}]}
