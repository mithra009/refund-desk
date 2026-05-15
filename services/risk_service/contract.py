"""The A2A contract. Deliberately duplicated in the main service (app/state.py RiskVerdict):
the services share a protocol, not a package. A contract test keeps the two in step."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RiskRequest(BaseModel):
    customer_id: str
    order_id: str | None = None
    claim_type: str | None = None


class RiskVerdict(BaseModel):
    level: Literal["low", "medium", "high"]
    signals: list[str] = Field(default_factory=list)
    score: float = Field(ge=0, le=1)


def level_for(score: float) -> str:
    """The thresholds are code, not model judgement."""
    return "high" if score >= 0.65 else "medium" if score >= 0.30 else "low"
