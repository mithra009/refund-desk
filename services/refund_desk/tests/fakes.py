"""Fakes for network-free graph tests: a tool-calling chat model and a scripted structured handler."""
from __future__ import annotations

import re
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.state import Critique, Decision, EvidenceOut, Intake, Plan, PlanItem
from app.nodes.reply import ReplyText
from evals.oracle import Reference


class ToolCallingFake(BaseChatModel):
    """Calls every bound tool once on its first turn (passing the order id it finds in the
    task), then answers. Counts invocations so tests can assert loop behaviour."""

    bound: list = []
    calls: int = 0
    fail: bool = False
    loop_forever: bool = False

    @property
    def _llm_type(self) -> str:
        return "tool-calling-fake"

    def bind_tools(self, tools, **kwargs):
        clone = self.model_copy()
        clone.bound = list(tools)
        return clone

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        object.__setattr__(self, "calls", self.calls + 1)
        if self.fail:
            raise RuntimeError("model down")
        has_results = any(isinstance(m, ToolMessage) for m in messages)
        if self.loop_forever or (not has_results and self.bound):
            order_id = None
            for m in messages:
                if isinstance(m, HumanMessage):
                    hit = re.search(r"Order id: (\S+)", str(m.content))
                    order_id = hit.group(1) if hit else order_id
            tcs = []
            for i, t in enumerate(self.bound):
                schema = t.args if hasattr(t, "args") else {}
                args = {"order_id": order_id} if "order_id" in schema else {}
                tcs.append({"name": t.name, "args": args, "id": f"call_{self.calls}_{i}"})
            msg = AIMessage(content="", tool_calls=tcs)
        else:
            msg = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def reference_decision(ref: Reference, evidence_ids: list[str], clauses: list[str]) -> Decision:
    """A 'perfect drafter': the oracle's answer, citing only ids that exist."""
    cited = [c for c in ref.clauses if c in clauses] or clauses[:1]
    return Decision(outcome=ref.outcome, amount_cents=ref.amount_cents, clauses=cited,
                    evidence_refs=evidence_ids[:2], rationale="scripted", confidence=0.95)


def make_handler(intake: Intake, decision_fn: Callable[[Any], Decision] | None = None,
                 critique_fn: Callable[[Any], Critique] | None = None,
                 plan: Plan | None = None, reply_text: str = "Your refund has been processed.",
                 log: list | None = None):
    """A structured-output handler keyed on the requested schema."""

    def handler(schema, messages, tier, name):
        if log is not None:
            log.append((schema.__name__, name))
        if schema is Intake:
            return intake
        if schema is Plan:
            return plan or Plan(items=[
                PlanItem(question="What are the order total and items?", worker="order"),
                PlanItem(question="What are the delivery dates and tracking events?", worker="shipment"),
                PlanItem(question="What is the customer's 90-day refund history?", worker="history")])
        if schema is EvidenceOut:
            text = str(messages[-1].content)
            return EvidenceOut(finding=("Facts: " + text[:300]).replace("\n", " "), confidence=0.9)
        if schema is Decision:
            return decision_fn(messages)
        if schema is Critique:
            return critique_fn(messages) if critique_fn else Critique(verdict="accept")
        if schema is ReplyText:
            return ReplyText(text=reply_text)
        raise AssertionError(f"unscripted schema {schema}")

    return handler


def draft_ids(messages) -> tuple[list[str], list[str]]:
    """Evidence ids and clause ids visible in a draft prompt."""
    text = str(messages[-1].content)
    return (re.findall(r'"id": "(ev_[a-z_0-9]+)"', text), re.findall(r'"id": "(R\d+)"', text))
