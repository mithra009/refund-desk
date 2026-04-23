# 1. The supervisor is plain code

Status: accepted. The top-level order of phases (investigate, retrieve policy, draft, guard) never varies, so routing is `if` statements in `nodes/supervisor.py`. Rejected: an LLM router, which adds latency, cost and variance to a decision that has one right answer. Model-driven delegation exists one level down, in the investigation lead, where what to look up does depend on the case.
