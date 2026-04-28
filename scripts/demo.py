"""Scripted demo against a running stack (`docker compose up`): three cases.

    python scripts/demo.py            # reads REVIEWER_TOKEN from the environment / .env

1. cus_demo_1  damaged mug, $29       -> auto-approved and refunded by the agent
2. cus_demo_2  damaged chair, $264    -> over the $100 authority: paused for review, a human approves
3. cus_demo_3  change of mind, day 38 -> denied (outside the 30-day window)
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("REFUND_DESK_URL", "http://localhost:8000")
REVIEWER = os.environ.get("REVIEWER_TOKEN", "")


def wait(c: httpx.Client, case_id: str, headers: dict, until: set[str], timeout: float = 180) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        body = c.get(f"/cases/{case_id}", headers=headers).json()
        if body["status"] in until:
            return body
        time.sleep(1)
    raise SystemExit(f"timed out waiting for {case_id}: {body}")


def main() -> None:
    staff = {"X-Reviewer-Token": REVIEWER}
    with httpx.Client(base_url=BASE, timeout=30) as c:
        for cust, msg, photo in [
            ("cus_demo_1", "My red mug arrived smashed. I'd like my money back.", True),
            ("cus_demo_2", "The ergonomic chair I ordered arrived with a snapped armrest. Please refund me.", True),
            ("cus_demo_3", "I changed my mind about the desk lamp and want to return it.", False),
        ]:
            h = {"X-Customer-Id": cust}
            case_id = c.post("/cases", json={"message": msg, "has_photo": photo}, headers=h).json()["case_id"]
            print(f"\n[{cust}] opened {case_id}")
            body = wait(c, case_id, staff, {"refunded", "closed", "pending_review", "reviewed", "failed"})
            if body["status"] == "pending_review":
                p = body["pending_review"]
                print(f"  paused for review: reasons={p['reasons']} draft={p['draft']['outcome']} {p['draft']['amount_cents']}c")
                body = c.post(f"/cases/{case_id}/review", json={"action": "approve", "reviewer": "demo-reviewer"},
                              headers=staff).json()
                print("  human approved")
            print(f"  status={body['status']} refund_id={body['refund_id']}")
            print(f"  reply: {body['reply']}")


if __name__ == "__main__":
    if not REVIEWER:
        sys.exit("set REVIEWER_TOKEN (same value the stack was started with)")
    main()
