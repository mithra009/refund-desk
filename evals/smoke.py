"""CI gate: run the 25-case smoke subset 3 times against config E and apply evals.gate.

    python -m evals.smoke                    # exit 0 = pass, 1 = fail
    python -m evals.smoke --update-baseline  # store the observed accuracy as the new baseline

Real model calls: needs GOOGLE_API_KEY in the environment.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from evals import gate
from evals.run import git_sha, load_records, run_local, summarize


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--update-baseline", action="store_true")
    a = ap.parse_args()
    records = gate.smoke_subset(load_records("dev"))
    rows = asyncio.run(run_local("E", records, a.repeats, judges=False))
    by_rep: dict[int, list] = {}
    for r in rows:
        by_rep.setdefault(r["rep"], []).append(r)
    accs = [summarize(v)["outcome_acc"] for v in by_rep.values()]
    unsafe = sum(summarize(v)["unsafe_actions"] for v in by_rep.values())
    print(f"runs={len(accs)} accuracies={[round(x, 3) for x in accs]} unsafe_total={unsafe}")
    if a.update_baseline:
        gate.store_baseline(sum(accs) / len(accs), git_sha())
        print("baseline stored")
        return 0 if unsafe == 0 else 1
    ok, msg = gate.check(accs, unsafe, gate.load_baseline())
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
