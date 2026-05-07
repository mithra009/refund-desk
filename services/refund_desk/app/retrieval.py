"""Hybrid policy retrieval: BM25 + dense vectors, fused with reciprocal rank fusion.

One clause, one chunk, with its id. The dense side is a deterministic hashing embedder
(no embedding provider needed); swap `embed` for a real model and re-run `index_to_db`.
In Postgres the vectors live in `policy_chunks` (pgvector) and are searched with `<=>`.
"""
from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

from app.guards import ALWAYS_CLAUSES
from app.state import PolicyClause

DIM = 256
RRF_K = 60
TOP_K = 6
POLICY_DIR = Path(os.environ.get("POLICY_DIR", Path(__file__).resolve().parents[3] / "data" / "policy"))

_HEADING = re.compile(r"^##\s+(R\d+)\s+[—-]\s+(.+?)\s*$", re.M)

CLAIM_WORDS = {
    "damaged": "damaged on arrival broken photo damaged scan",
    "not_delivered": "not delivered never arrived lost promised delivery date",
    "missing_after_delivery": "marked delivered missing parcel not received tracking delivered",
    "late": "late delivery delivered after promised shipping fee",
    "changed_mind": "change of mind return window items",
    "other": "unclassifiable request",
}


def load_clauses(directory: Path = POLICY_DIR) -> list[PolicyClause]:
    clauses: list[PolicyClause] = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        marks = list(_HEADING.finditer(text))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            clauses.append(PolicyClause(id=m.group(1), title=m.group(2), text=text[m.end():end].strip()))
    clauses.sort(key=lambda c: int(c.id[1:]))
    return clauses


def tokenize(s: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9$]+", s.lower()):
        for suf in ("ing", "ed", "es", "s"):
            if len(w) > len(suf) + 3 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.append(w)
    return out


def embed(text: str) -> np.ndarray:
    toks = tokenize(text)
    v = np.zeros(DIM, dtype=np.float32)
    for t in toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]:
        h = int.from_bytes(hashlib.blake2b(t.encode(), digest_size=8).digest(), "big")
        v[h % DIM] += 1.0 if (h >> 63) & 1 else -1.0
    n = np.linalg.norm(v)
    return v / n if n else v


def _vec_literal(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


class PolicyIndex:
    def __init__(self, clauses: list[PolicyClause] | None = None, db=None):
        self.clauses = clauses or load_clauses()
        self.by_id = {c.id: c for c in self.clauses}
        self.bm25 = BM25Okapi([tokenize(f"{c.title} {c.text}") for c in self.clauses])
        self.matrix = np.stack([embed(f"{c.title} {c.text}") for c in self.clauses])
        self.db = db if (db is not None and not db.is_sqlite) else None

    def _dense_rank(self, query: str) -> list[int]:
        q = embed(query)
        if self.db is not None:
            rows = self.db.all("SELECT clause_id FROM policy_chunks ORDER BY embedding <=> %s::vector LIMIT %s",
                               (_vec_literal(q), len(self.clauses)))
            if rows:
                idx = {c.id: i for i, c in enumerate(self.clauses)}
                return [idx[r["clause_id"]] for r in rows if r["clause_id"] in idx]
        return list(np.argsort(-(self.matrix @ q)))

    def search(self, query: str, k: int = TOP_K) -> list[PolicyClause]:
        sparse = self.bm25.get_scores(tokenize(query))
        sparse_rank = [int(i) for i in np.argsort(-sparse)]
        dense_rank = [int(i) for i in self._dense_rank(query)]
        fused: dict[int, float] = {}
        for ranking in (sparse_rank, dense_rank):
            for r, i in enumerate(ranking):
                fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + r + 1)
        top = sorted(fused, key=lambda i: -fused[i])[:k]
        hits = [self.clauses[i].model_copy(update={"score": round(fused[i], 5)}) for i in top]
        have = {h.id for h in hits}
        for cid in ALWAYS_CLAUSES:            # authority clauses never depend on recall
            if cid in self.by_id and cid not in have:
                hits.append(self.by_id[cid].model_copy(update={"score": 0.0}))
        return hits


def build_query(claim_type: str, findings: list[str]) -> str:
    """Built from the claim type and evidence findings, never from the raw customer message."""
    return " ".join([CLAIM_WORDS.get(claim_type, claim_type), *findings])


def index_to_db(db, index: PolicyIndex | None = None) -> int:
    """Write clause chunks (and embeddings) to policy_chunks."""
    index = index or PolicyIndex()
    db.execute("DELETE FROM policy_chunks")
    for c, vec in zip(index.clauses, index.matrix):
        emb = _vec_literal(vec)
        sql = ("INSERT INTO policy_chunks (clause_id, title, body, embedding) VALUES (%s,%s,%s,%s)"
               if db.is_sqlite else
               "INSERT INTO policy_chunks (clause_id, title, body, embedding) VALUES (%s,%s,%s,%s::vector)")
        db.execute(sql, (c.id, c.title, c.text, emb))
    return len(index.clauses)


@lru_cache(maxsize=1)
def get_index() -> PolicyIndex:
    from backend_sim.db import get_db
    return PolicyIndex(db=get_db())
