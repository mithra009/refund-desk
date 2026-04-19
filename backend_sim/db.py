"""Thin database layer: Postgres in production, SQLite in unit tests.

SQL is written once with %s placeholders. The SQLite backend rewrites them and
strips the Postgres-only bits of schema.sql, so tests run with no server.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def parse_ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(v))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Db:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.is_sqlite = dsn.startswith("sqlite")
        self._lock = threading.RLock()
        if self.is_sqlite:
            path = dsn.removeprefix("sqlite:///") or ":memory:"
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._pool = None
        else:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
            self._pool = ConnectionPool(dsn, min_size=1, max_size=10, open=True,
                                        kwargs={"row_factory": dict_row, "autocommit": True})

    # ---- value adapters -------------------------------------------------
    def ts(self, dt: datetime | None):
        if dt is None:
            return None
        return dt.isoformat() if self.is_sqlite else dt

    def json(self, obj: Any):
        if self.is_sqlite:
            return json.dumps(obj, default=str)
        from psycopg.types.json import Jsonb
        return Jsonb(obj, dumps=lambda o: json.dumps(o, default=str))

    @staticmethod
    def parse_json(v: Any) -> Any:
        return json.loads(v) if isinstance(v, (str, bytes)) else v

    # ---- execution -------------------------------------------------------
    def _sql(self, sql: str) -> str:
        return sql.replace("%s", "?") if self.is_sqlite else sql

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        """Run a write; returns affected row count."""
        if self.is_sqlite:
            with self._lock:
                cur = self._conn.execute(self._sql(sql), tuple(params))
                self._conn.commit()
                return cur.rowcount
        with self._pool.connection() as c:
            return c.execute(sql, params).rowcount

    def all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        if self.is_sqlite:
            with self._lock:
                cur = self._conn.execute(self._sql(sql), tuple(params))
                return [dict(r) for r in cur.fetchall()]
        with self._pool.connection() as c:
            return list(c.execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    # ---- schema ----------------------------------------------------------
    def init_schema(self) -> None:
        text = SCHEMA_PATH.read_text(encoding="utf-8")
        if self.is_sqlite:
            text = re.sub(r"CREATE EXTENSION[^;]*;", "", text)
            text = text.replace("TIMESTAMPTZ", "TEXT").replace("JSONB", "TEXT")
            text = text.replace("vector(256)", "TEXT")
            text = re.sub(r"--[^\n]*", "", text)
            with self._lock:
                self._conn.executescript(text)
            return
        with self._pool.connection() as c:
            c.execute(text)

    def reset(self) -> None:
        tables = ["audit_log", "cases", "refunds", "carrier_claims", "tracking_events",
                  "shipments", "order_items", "orders", "customers", "policy_chunks"]
        for t in tables:
            self.execute(f"DELETE FROM {t}")


_default: Db | None = None


def get_db() -> Db:
    """Process-wide handle. DATABASE_URL: postgresql://... or sqlite:///path."""
    global _default
    if _default is None:
        _default = Db(os.environ.get("DATABASE_URL", "sqlite:///:memory:"))
        _default.init_schema()
    return _default


def set_db(db: Db | None) -> None:
    global _default
    _default = db
