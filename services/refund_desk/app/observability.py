"""LangSmith wiring: a redacting client and the tags every run carries."""
from __future__ import annotations

import os
from functools import lru_cache

from app.config import cfg
from app.redact import redact


@lru_cache(maxsize=1)
def langsmith_client():
    """A LangSmith client that redacts PII from inputs and outputs. None when no key is set."""
    if not os.environ.get("LANGSMITH_API_KEY"):
        return None
    from langsmith import Client
    return Client(hide_inputs=redact, hide_outputs=redact)


def run_tags(case_id: str, claim_type: str | None = None) -> dict:
    """RunnableConfig fragment: tags + metadata for every run of a case."""
    meta = {"case_id": case_id, "prompt_version": cfg.prompt_version, "git_sha": cfg.git_sha}
    tags = [f"case:{case_id}", f"prompt:{cfg.prompt_version}", f"git:{cfg.git_sha}"]
    if claim_type:
        meta["claim_type"] = claim_type
        tags.append(f"claim:{claim_type}")
    return {"tags": tags, "metadata": meta}


def traceable(name: str | None = None, **kw):
    """langsmith.traceable that degrades to a no-op if langsmith is unavailable."""
    try:
        from langsmith import traceable as _t
        return _t(name=name, **kw)
    except Exception:  # pragma: no cover
        return lambda f: f
