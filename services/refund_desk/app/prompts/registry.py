"""Prompt lookup: LangSmith-versioned when REFUND_DESK_PROMPT_VERSION names a version, else local."""
from __future__ import annotations

import logging
import os
from functools import lru_cache

from app.config import cfg
from app.prompts.templates import ALL

log = logging.getLogger(__name__)
REPO_PREFIX = "refund-desk-"


@lru_cache(maxsize=None)
def get_prompt(name: str) -> str:
    """System text for `name`. The deployed version id is config and a trace tag, so a
    regression can be tied to a prompt change."""
    if cfg.prompt_version != "local" and os.environ.get("LANGSMITH_API_KEY"):
        try:
            from langsmith import Client
            prompt = Client().pull_prompt(f"{REPO_PREFIX}{name}:{cfg.prompt_version}")
            return prompt.messages[0].prompt.template.replace("{{", "{").replace("}}", "}")
        except Exception as e:  # network, missing version
            log.warning("prompt %s@%s unavailable (%s); using local copy", name, cfg.prompt_version, e)
    return ALL[name]


def render(name: str, **values: str) -> str:
    text = get_prompt(name)
    for k, v in values.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def push_prompts() -> None:
    """Publish the local prompts to LangSmith as new versions (run from CI or by hand)."""
    from langchain_core.prompts import ChatPromptTemplate
    from langsmith import Client
    client = Client()
    for name, text in ALL.items():
        escaped = text.replace("{", "{{").replace("}", "}}")
        client.push_prompt(f"{REPO_PREFIX}{name}", object=ChatPromptTemplate.from_messages([("system", escaped)]))


if __name__ == "__main__":
    push_prompts()
