"""Model access. Nodes never import a provider; they ask for a tier.

Test seams: `set_structured_handler` replaces every structured call with a scripted
function; `set_chat_model` replaces the tool-calling chat model for a tier.
"""
from __future__ import annotations

from typing import Callable, TypeVar

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.rate_limiters import InMemoryRateLimiter
from pydantic import BaseModel

from app.config import cfg

T = TypeVar("T", bound=BaseModel)

_chat_overrides: dict[str, BaseChatModel] = {}
_structured_handler: Callable[..., BaseModel] | None = None


def set_chat_model(tier: str, model: BaseChatModel | None) -> None:
    if model is None:
        _chat_overrides.pop(tier, None)
    else:
        _chat_overrides[tier] = model


def set_structured_handler(fn: Callable[..., BaseModel] | None) -> None:
    """fn(schema, messages, tier, name) -> schema instance."""
    global _structured_handler
    _structured_handler = fn


def _model_id(tier: str) -> str:
    return {"fast": cfg.fast, "strong": cfg.strong, "critic": cfg.critic_model_id,
            "fast_fallback": cfg.fast_fallback, "strong_fallback": cfg.strong_fallback}[tier]


_limiters: dict[str, InMemoryRateLimiter] = {}


def _limiter(model_id: str) -> dict:
    """One shared client-side limiter per model id (provider quotas are per model)."""
    if cfg.requests_per_minute is None:
        return {}
    if model_id not in _limiters:
        _limiters[model_id] = InMemoryRateLimiter(requests_per_second=cfg.requests_per_minute / 60,
                                                  check_every_n_seconds=0.1, max_bucket_size=1)
    return {"rate_limiter": _limiters[model_id]}


def _sampling() -> dict:
    return {} if cfg.temperature is None else {"temperature": cfg.temperature}


def get_model(tier: str) -> BaseChatModel:
    if tier in _chat_overrides:
        return _chat_overrides[tier]
    return init_chat_model(_model_id(tier), timeout=cfg.model_timeout_s, max_retries=4, **_sampling(),
                           **_limiter(_model_id(tier)))


def fallback_model(tier: str) -> BaseChatModel:
    key = f"{tier}_fallback"
    if key in _chat_overrides:
        return _chat_overrides[key]
    mid = _model_id("strong_fallback" if tier == "strong" else "fast_fallback")
    return init_chat_model(mid, timeout=cfg.model_timeout_s, max_retries=4, **_sampling(), **_limiter(mid))


def _usage(raw) -> int:
    meta = getattr(raw, "usage_metadata", None) or {}
    return int(meta.get("total_tokens", 0))


def structured(tier: str, schema: type[T], messages: list, name: str = "") -> tuple[T, int]:
    """One model call returning a validated `schema` and the tokens it cost.

    Falls back to the tier's fallback model on provider errors. A parse failure raises,
    so the node's RetryPolicy gets another attempt.
    """
    if _structured_handler is not None:
        return _structured_handler(schema, messages, tier, name), 0

    def _call(model: BaseChatModel):
        out = model.with_structured_output(schema, include_raw=True).invoke(
            messages, config={"run_name": name or schema.__name__})
        if out.get("parsing_error") is not None or out.get("parsed") is None:
            raise ValueError(f"{schema.__name__}: unparseable model output: {out.get('parsing_error')}")
        return out["parsed"], _usage(out.get("raw"))

    try:
        return _call(get_model(tier))
    except ValueError:
        raise
    except Exception:  # provider outage, rate limit, timeout
        return _call(fallback_model("strong" if tier in ("strong", "critic") else "fast"))
