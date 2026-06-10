"""A2A client for the risk service. The main graph knows only this contract.

5 second timeout, 2 retries with jitter, a circuit breaker. Any failure returns None and the
guard treats `risk is None` as a reason to escalate: a broken specialist makes the system more
cautious, never less.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid

import httpx
from a2a.client import A2ACardResolver, ClientCallContext, ClientConfig, create_client
from a2a.types import a2a_pb2

from app.config import cfg
from app.observability import traceable
from app.state import RiskVerdict

log = logging.getLogger(__name__)


class CircuitBreaker:
    """Opens after `threshold` consecutive failures; half-opens after `reset_s`."""

    def __init__(self, threshold: int, reset_s: float, clock=time.monotonic):
        self.threshold, self.reset_s, self.clock = threshold, reset_s, clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self.opened_at >= self.reset_s else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold or self.state == "half_open":
            self.opened_at = self.clock()


def pin_card_url(card, base_url: str):
    """Make the client connect to the URL we were configured with, not the address the card
    advertises. A card served inside Docker names an internal host (risk-service) that a client
    on the host machine cannot resolve."""
    for iface in card.supported_interfaces:
        iface.url = base_url.rstrip("/")
    return card


class RiskUnavailable(Exception):
    pass


def _texts(resp: a2a_pb2.StreamResponse) -> list[str]:
    out: list[str] = []

    def parts(ps):
        out.extend(p.text for p in ps if p.text)

    kind = resp.WhichOneof("payload")
    if kind == "message":
        parts(resp.message.parts)
    elif kind == "task":
        for a in resp.task.artifacts:
            parts(a.parts)
        parts(resp.task.status.message.parts)
    elif kind == "artifact_update":
        parts(resp.artifact_update.artifact.parts)
    elif kind == "status_update":
        parts(resp.status_update.status.message.parts)
    return out


def parse_verdict(texts: list[str]) -> RiskVerdict:
    for t in reversed(texts):
        try:
            data = json.loads(t)
        except ValueError:
            continue
        if isinstance(data, dict) and "level" in data:
            return RiskVerdict.model_validate(data)
        if isinstance(data, dict) and "error" in data:
            raise RiskUnavailable(str(data["error"]))
    raise RiskUnavailable("no verdict in response")


class RiskClient:
    def __init__(self, base_url: str | None = None, *, timeout_s: float | None = None,
                 retries: int | None = None, breaker: CircuitBreaker | None = None,
                 httpx_client: httpx.AsyncClient | None = None):
        self.base_url = base_url or cfg.risk_service_url
        self.timeout_s = cfg.risk_timeout_s if timeout_s is None else timeout_s
        self.retries = cfg.risk_retries if retries is None else retries
        self.breaker = breaker or CircuitBreaker(cfg.breaker_failures, cfg.breaker_reset_s)
        self._http = httpx_client

    async def _once(self, request_json: str, headers: dict[str, str]) -> RiskVerdict:
        owns = self._http is None
        http = self._http or httpx.AsyncClient(timeout=self.timeout_s)
        try:
            card = pin_card_url(await A2ACardResolver(http, self.base_url).get_agent_card(), self.base_url)
            client = await create_client(card, client_config=ClientConfig(httpx_client=http, streaming=False))
            msg = a2a_pb2.Message(message_id=uuid.uuid4().hex, role=a2a_pb2.ROLE_USER,
                                  parts=[a2a_pb2.Part(text=request_json)])
            ctx = ClientCallContext(service_parameters=headers, timeout=self.timeout_s)
            texts: list[str] = []
            async for resp in client.send_message(a2a_pb2.SendMessageRequest(message=msg), context=ctx):
                texts.extend(_texts(resp))
            return parse_verdict(texts)
        finally:
            if owns:
                await http.aclose()

    @traceable(name="risk_service_call")
    async def assess(self, customer_id: str, order_id: str | None, claim_type: str | None,
                     headers: dict[str, str] | None = None) -> RiskVerdict | None:
        if not self.breaker.allow():
            log.warning("risk circuit open; skipping call")
            return None
        body = json.dumps({"customer_id": customer_id, "order_id": order_id, "claim_type": claim_type})
        hdrs = headers or {}
        for attempt in range(self.retries + 1):
            try:
                verdict = await asyncio.wait_for(self._once(body, hdrs), timeout=self.timeout_s)
                self.breaker.success()
                return verdict
            except Exception as e:
                log.warning("risk call failed (attempt %d): %s", attempt + 1, type(e).__name__)
                if attempt < self.retries:
                    await asyncio.sleep(random.uniform(0, 0.25 * (2 ** attempt)))
        self.breaker.failure()
        return None


_client: RiskClient | None = None


def get_risk_client() -> RiskClient:
    global _client
    if _client is None:
        _client = RiskClient()
    return _client


def set_risk_client(c: RiskClient | None) -> None:
    global _client
    _client = c


def trace_headers() -> dict[str, str]:
    """Propagate the LangSmith trace so ADK's OTel spans land under the same trace."""
    headers: dict[str, str] = {}
    try:
        from langsmith.run_helpers import get_current_run_tree
        rt = get_current_run_tree()
        if rt is not None:
            headers.update(rt.to_headers())       # langsmith-trace, baggage
    except Exception:
        pass
    return headers
