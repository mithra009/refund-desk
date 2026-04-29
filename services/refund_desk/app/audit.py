"""Business audit trail in Postgres, independent of LangSmith."""
from __future__ import annotations

from datetime import datetime, timezone

from backend_sim.db import get_db


def write(case_id: str, event: str, payload: dict | None = None, actor: str = "refund_desk") -> None:
    db = get_db()
    db.execute("INSERT INTO audit_log (case_id, ts, actor, event, payload) VALUES (%s,%s,%s,%s,%s)",
               (case_id, db.ts(datetime.now(timezone.utc)), actor, event, db.json(payload or {})))


def read(case_id: str) -> list[dict]:
    db = get_db()
    rows = db.all("SELECT ts, actor, event, payload FROM audit_log WHERE case_id = %s ORDER BY ts", (case_id,))
    for r in rows:
        r["payload"] = db.parse_json(r["payload"])
    return rows
