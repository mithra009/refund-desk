"""The risk specialist, built in Google ADK.

gather (parallel code-run signal collectors) -> refine (scorer/reviewer loop) -> emit (code).
ADK workflow agents wire this declaratively; session state + output_key pass data between agents.
"""
from __future__ import annotations

import json
import os
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent, LoopAgent, ParallelAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.tools import ToolContext
from google.genai import types
from pydantic import BaseModel, Field

from risk_service.contract import RiskVerdict, level_for
from risk_service.tools import account_age, address_reuse, payment_changes, refund_velocity

# Native Gemini through ADK; reads GOOGLE_API_KEY from the environment. Confirm the id exists for your key.
RISK_LLM = os.environ.get("RISK_MODEL", "gemini-3.1-flash-lite-preview")

class ScorerDraft(BaseModel):
    """Whole-number points: lite models loop on long decimals, so no floats cross this boundary."""
    score_pct: int = Field(ge=0, le=100, description="Total points, 0 to 100 (0.35 is 35)")
    signals: list[str] = Field(default_factory=list, description="Names of the rules that fired")


SCORER_PROMPT = """You score fraud/abuse risk from signals. Velocity signals: {velocity_signals}
Account signals: {account_signals}

Score additively in whole points, then cap at 100:
- account_age_days < 14: +35
- payment_changes_30d >= 2: +30
- address_reuse_accounts >= 3: +35
- refunds_last_90d >= 2: +15
Return `score_pct` (an integer, no decimals) and `signals`: the names of the rules that fired, e.g.
"new_account". If a signal is missing, treat it as not fired and add "missing:<name>" to signals.
{last_review}"""

REVIEWER_PROMPT = """You check a risk draft against the signals it was computed from.
Signals: {velocity_signals} / {account_signals}
Draft: {risk_draft}
If score_pct is the correct sum of the points of the fired rules, call the
`accept` tool. Otherwise explain the exact error in one sentence and do not call it."""


def accept(tool_context: ToolContext) -> dict:
    """Call when the risk draft is consistent with the signals."""
    tool_context.actions.escalate = True       # leaves the LoopAgent
    return {}


# Free-tier quotas return 429; retry with backoff instead of failing the whole assessment.
RETRY = types.GenerateContentConfig(http_options=types.HttpOptions(
    retry_options=types.HttpRetryOptions(attempts=6, initial_delay=2.0, max_delay=30.0, exp_base=2.0)))

# A runaway generation fails fast instead of producing 60k characters.
SCORER_CONFIG = types.GenerateContentConfig(max_output_tokens=400, http_options=RETRY.http_options)

class SignalCollector(BaseAgent):
    """Non-LLM signal collection. Reading four database values needs no judgement, and a lite model
    that must call tools and relay the results proved unreliable (it returned nothing, which then
    scored as low risk). Writes `name=value` lines to session state, as the scorer expects.
    A signal that cannot be read becomes `missing:<name>`, which the service treats as a failure."""

    tool_fns: list = []
    state_key: str = ""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        lines: list[str] = []
        try:
            customer_id = json.loads(ctx.user_content.parts[0].text)["customer_id"]
        except Exception:
            customer_id = None
        for fn in self.tool_fns:
            result = fn(customer_id) if customer_id else {"error": "no customer_id"}
            if "error" in result:
                lines.append(f"missing:{fn.__name__}")
            else:
                lines += [f"{k}={v}" for k, v in result.items() if k != "customer_id"]
        text = chr(10).join(lines)
        yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part(text=text)]),
                    actions=EventActions(state_delta={self.state_key: text}))


velocity = SignalCollector(name="velocity", tool_fns=[refund_velocity, address_reuse],
                           state_key="velocity_signals")
account = SignalCollector(name="account", tool_fns=[account_age, payment_changes],
                          state_key="account_signals")

gather = ParallelAgent(name="gather", sub_agents=[velocity, account])

# output_schema and tools do not mix on an LlmAgent, so the scorer has no tools of its own.
scorer = LlmAgent(name="scorer", model=RISK_LLM, instruction=SCORER_PROMPT.replace("{last_review}", "{risk_review?}"),
                  output_schema=ScorerDraft, output_key="risk_draft",
                  generate_content_config=SCORER_CONFIG)

reviewer = LlmAgent(name="reviewer", model=RISK_LLM, generate_content_config=RETRY, instruction=REVIEWER_PROMPT,
                    tools=[accept], output_key="risk_review")

refine = LoopAgent(name="refine", sub_agents=[scorer, reviewer], max_iterations=2)


def normalize(raw) -> RiskVerdict:
    """Level is derived from the score in code; the model only supplies score and signals."""
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    missing = [s for s in data.get("signals", []) if str(s).startswith("missing:")]
    if missing:      # no data is not "low risk": fail closed so the caller escalates
        raise ValueError(f"signals unavailable: {missing}")
    score = float(data["score"]) if "score" in data else int(data["score_pct"]) / 100
    score = max(0.0, min(1.0, score))
    return RiskVerdict(level=level_for(score), signals=[str(s) for s in data.get("signals", [])],
                       score=round(score, 2))


class VerdictEmitter(BaseAgent):
    """Non-LLM final step: emit the verdict as the response, validated and normalised."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        try:
            text = normalize(ctx.session.state["risk_draft"]).model_dump_json()
        except Exception as e:
            text = json.dumps({"error": f"risk_unavailable: {type(e).__name__}"})
        yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part(text=text)]))


emit = VerdictEmitter(name="emit")

root_agent = SequentialAgent(
    name="risk_service",
    description=("Fraud/abuse risk assessment for refund requests. Input: JSON {customer_id, order_id, "
                 "claim_type}. Output: JSON {level: low|medium|high, signals: [str], score: 0..1}."),
    sub_agents=[gather, refine, emit])
