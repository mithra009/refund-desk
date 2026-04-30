"""The feedback loop: every human edit or rejection becomes a candidate evaluation example.

With LANGSMITH_API_KEY set the example goes to the `refund-desk-feedback` dataset; the reviewer's
answer is the reference. Without a key it is appended to data/feedback/reviews.jsonl, so nothing
is lost. From the feedback dataset, vetted examples are promoted into `refund-desk-dev`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.redact import redact

DATASET = os.environ.get("FEEDBACK_DATASET", "refund-desk-feedback")
LOCAL = Path(os.environ.get("FEEDBACK_DIR", Path(__file__).resolve().parents[3] / "data" / "feedback"))


def record_review(case_id: str, summary: str | None, draft: dict | None, review: dict,
                  final: dict | None) -> str:
    """Returns where the example went: 'langsmith' or 'local'. No-op for plain approvals."""
    if review["action"] == "approve":
        return "none"
    inputs = redact({"case_id": case_id, "summary": summary, "agent_draft": draft})
    outputs = {"reviewer_action": review["action"], "reference_decision": final,
               "note": redact(review.get("note", ""))}
    if os.environ.get("LANGSMITH_API_KEY"):
        from langsmith import Client
        client = Client()
        if not client.has_dataset(dataset_name=DATASET):
            client.create_dataset(DATASET, description="Human edits and rejections from review")
        client.create_example(dataset_name=DATASET, inputs=inputs, outputs=outputs,
                              metadata={"case_id": case_id, "source": "human_review"})
        return "langsmith"
    LOCAL.mkdir(parents=True, exist_ok=True)
    with (LOCAL / "reviews.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"inputs": inputs, "outputs": outputs}, default=str) + "\n")
    return "local"
