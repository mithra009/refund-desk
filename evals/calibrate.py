"""Calibrate the LLM judges against hand labels.

    python -m evals.calibrate export results.json --n 30     # writes data/judge_labels.todo.jsonl to label
    python -m evals.calibrate report                         # agreement of each judge with data/judge_labels.jsonl

Labels are yours to write: set "groundedness" and "reply_quality" to true/false per row. Report the
agreement rate next to every judge score; an uncalibrated judge is a number with no meaning.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
TODO, LABELS = DATA / "judge_labels.todo.jsonl", DATA / "judge_labels.jsonl"


def export(results_path: str, n: int, seed: int = 3) -> Path:
    rows = [r for r in json.loads(Path(results_path).read_text(encoding="utf-8"))
            if r["outputs"].get("decision") and r["outputs"].get("reply")]
    sample = random.Random(seed).sample(rows, min(n, len(rows)))
    with TODO.open("w", encoding="utf-8") as fh:
        for r in sample:
            fh.write(json.dumps({"id": r["id"], "rep": r["rep"], "outputs": r["outputs"],
                                 "judge_groundedness": r["scores"].get("groundedness"),
                                 "judge_reply_quality": r["scores"].get("reply_quality"),
                                 "groundedness": None, "reply_quality": None}) + "\n")
    return TODO


def agreement(rows: list[dict]) -> dict:
    out = {}
    for key in ("groundedness", "reply_quality"):
        pairs = [(r[key], r[f"judge_{key}"]) for r in rows if r.get(key) is not None and r.get(f"judge_{key}") is not None]
        out[key] = {"n": len(pairs), "agreement": (sum(a == bool(b) for a, b in pairs) / len(pairs)) if pairs else None}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("results")
    e.add_argument("--n", type=int, default=30)
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "export":
        print(f"wrote {export(a.results, a.n)}; label it, save as {LABELS}")
    else:
        rows = [json.loads(line) for line in LABELS.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(json.dumps(agreement(rows), indent=1))


if __name__ == "__main__":
    main()
