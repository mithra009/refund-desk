"""Long-term memory: a LangGraph Store keyed by customer_id, a few factual lines per case.

In production `set_store(AsyncPostgresStore)` is called at startup; tests and the
default use the in-memory store.
"""
from __future__ import annotations

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

_store: BaseStore = InMemoryStore()


def set_store(store: BaseStore) -> None:
    global _store
    _store = store


def get_store() -> BaseStore:
    return _store


def read_memory(customer_id: str, limit: int = 20) -> list[str]:
    items = _store.search(("customers", customer_id), limit=limit)
    return [i.value["note"] for i in sorted(items, key=lambda i: i.value.get("seq", 0))]


def write_memory(customer_id: str, case_id: str, note: str) -> None:
    n = len(_store.search(("customers", customer_id), limit=1000))
    _store.put(("customers", customer_id), case_id, {"note": note, "seq": n})
