"""CaseState and every pydantic contract that crosses a node boundary."""
from __future__ import annotations

import operator
import time
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

from app.config import cfg

Outcome = Literal["approve_full", "approve_partial", "deny", "escalate"]
ClaimType = Literal["damaged", "not_delivered", "missing_after_delivery",
                    "late", "changed_mind", "other"]


# ---- intake -------------------------------------------------------------------
class Intake(BaseModel):
    claim_type: ClaimType
    order_id: str | None = None
    requested_amount_cents: int | None = None
    has_photo: bool = False
    summary: str = Field(description="Neutral restatement of the claim, at most 40 words")


# ---- evidence -----------------------------------------------------------------
class Evidence(TypedDict):
    id: str                 # "ev_order_1"
    source: str             # worker or service that produced it
    question: str           # the plan item it answers
    finding: str            # one or two sentences
    raw: dict               # the tool output it rests on
    confidence: float


class EvidenceOut(BaseModel):
    """What a worker's finalize step produces from its tool results."""
    finding: str = Field(description="One or two sentences answering the question from tool results only")
    confidence: float = Field(ge=0, le=1)


class PlanItem(BaseModel):
    question: str
    worker: Literal["order", "shipment", "history", "all"]


class Plan(BaseModel):
    items: list[PlanItem]


class RiskVerdict(BaseModel):
    level: Literal["low", "medium", "high"]
    signals: list[str] = Field(default_factory=list)
    score: float = Field(ge=0, le=1)


class PolicyClause(BaseModel):
    id: str
    title: str
    text: str
    score: float = 0.0


# ---- decision -----------------------------------------------------------------
class Decision(BaseModel):
    outcome: Outcome
    amount_cents: int = Field(ge=0)
    clauses: list[str]              # policy clause ids relied on, e.g. ["R2", "R7"]
    evidence_refs: list[str]        # ids of Evidence items relied on
    rationale: str                  # internal, goes to the audit log
    confidence: float = Field(ge=0, le=1)


class Issue(BaseModel):
    kind: Literal["clause_missing", "claim_ungrounded", "amount_wrong", "clause_ignored", "other"]
    detail: str
    fix: str


class Critique(BaseModel):
    verdict: Literal["accept", "revise"]
    issues: list[Issue] = Field(default_factory=list)
    # rubric: four yes/no checks
    clauses_exist: bool = True
    claims_grounded: bool = True
    amount_follows: bool = True
    nothing_ignored: bool = True
    source: Literal["code", "model"] = "model"


class GuardResult(BaseModel):
    reasons: list[str]

    @classmethod
    def budget(cls) -> "GuardResult":
        return cls(reasons=["budget_exhausted"])


class HumanReview(BaseModel):
    action: Literal["approve", "edit", "reject"]
    outcome: Outcome | None = None       # for edit
    amount_cents: int | None = None      # for edit
    reviewer: str = "reviewer"
    note: str = ""


# ---- budget -------------------------------------------------------------------
class Budget(BaseModel):
    tokens: int = 0
    tool_calls: int = 0
    steps: int = 0
    started_at: float = Field(default_factory=time.time)

    @property
    def which_exhausted(self) -> str | None:
        if self.tokens >= cfg.max_tokens_per_case:
            return "tokens"
        if self.tool_calls >= cfg.max_tool_calls_per_case:
            return "tool_calls"
        if time.time() - self.started_at >= cfg.max_wall_clock_s:
            return "wall_clock"
        return None

    @property
    def exhausted(self) -> bool:
        return (self.tokens >= cfg.max_tokens_per_case
                or self.tool_calls >= cfg.max_tool_calls_per_case
                or time.time() - self.started_at >= cfg.max_wall_clock_s)

    def spend(self, tokens: int = 0, tool_calls: int = 0, steps: int = 0) -> "Budget":
        return self.model_copy(update={"tokens": self.tokens + tokens,
                                       "tool_calls": self.tool_calls + tool_calls,
                                       "steps": self.steps + steps})


class Spent(TypedDict, total=False):
    tokens: int
    tool_calls: int


# ---- case state ---------------------------------------------------------------
class CaseState(TypedDict, total=False):
    case_id: str
    customer_id: str                       # from the authenticated session, never from the message
    message: str                           # untrusted
    has_photo: bool                        # attachment flag from the API, not from the model
    intake: Intake | None
    plan: list[PlanItem]
    redispatched: bool
    spent_folded: int                      # how many `spent` entries merge has already counted
    evidence: Annotated[list[Evidence], operator.add]   # reducer: parallel workers append
    spent: Annotated[list[Spent], operator.add]         # workers report here; merge folds it into budget
    risk: RiskVerdict | None
    policy_hits: list[PolicyClause]
    draft: Decision | None
    critiques: Annotated[list[Critique], operator.add]
    revision_count: int
    guard: GuardResult | None
    human: HumanReview | None
    refund_id: str | None
    final: Decision | None                 # the decision actually acted on (after human edit)
    reply: str | None
    budget: Budget                         # tokens and steps spent so far


# Private input to workers: one question and ids, never the customer message.
class WorkerInput(TypedDict):
    question: str
    order_id: str | None
    customer_id: str
    worker: str
    index: int
