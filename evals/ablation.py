"""The ablation (IMPLEMENTATION.md section 12): five configurations, same split, N runs each.

    python -m evals.ablation run --split dev --repeats 3        # runs A..E (real model calls, costs money)
    python -m evals.ablation table evals/results/*.json         # builds the table from saved runs only

The table is built only from saved experiment output. Differences that fall inside run-to-run spread
are reported as "no measurable difference".
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
from collections import defaultdict
from pathlib import Path

from evals.run import CONFIGS, load_records, run_local, save, summarize

METRICS = [("outcome_acc", "Outcome acc.", "{:.3f}"), ("mean_amount_error", "Mean amount error (c)", "{:.0f}"),
           ("unsafe_actions", "Unsafe actions", "{:.0f}"), ("escalation_rate", "Escalation rate", "{:.3f}"),
           ("cost_per_case", "Cost / case ($)", "{:.4f}"), ("p95_latency", "p95 latency (s)", "{:.1f}")]


def per_rep(rows: list[dict]) -> list[dict]:
    reps = defaultdict(list)
    for r in rows:
        reps[r["rep"]].append(r)
    return [summarize(reps[k]) for k in sorted(reps)]


def stat(values: list[float]) -> tuple[float, float]:
    """(mean, spread) where spread is max - min across runs."""
    return sum(values) / len(values), (max(values) - min(values)) if len(values) > 1 else 0.0


def build_table(rows_by_config: dict[str, list[dict]]) -> str:
    stats: dict[str, dict[str, tuple[float, float]]] = {}
    for cfg_name, rows in rows_by_config.items():
        runs = per_rep(rows)
        stats[cfg_name] = {k: stat([r[k] for r in runs]) for k, _, _ in METRICS}
        stats[cfg_name]["_runs"] = (len(runs), 0.0)
    head = "| Config | " + " | ".join(label for _, label, _ in METRICS) + " | Runs |"
    lines = [head, "|" + "---|" * (len(METRICS) + 2)]
    for name in sorted(stats):
        cells = []
        for key, _, fmt in METRICS:
            m, s = stats[name][key]
            cells.append(fmt.format(m) + (f" ±{fmt.format(s)}" if stats[name]["_runs"][0] > 1 and s else ""))
        lines.append(f"| {name}. {CONFIGS[name]} | " + " | ".join(cells) + f" | {int(stats[name]['_runs'][0])} |")

    notes = []
    if "E" in stats:
        for name in sorted(stats):
            if name == "E":
                continue
            for key, label, _ in METRICS[:2] + METRICS[3:4]:
                (m, s), (me, se) = stats[name][key], stats["E"][key]
                if abs(m - me) <= max(s, se):
                    notes.append(f"- {name} vs E, {label}: no measurable difference "
                                 f"(gap {abs(m - me):.4g} within spread {max(s, se):.4g}).")
    if all(stats[n]["cost_per_case"][0] == 0 for n in stats):
        notes.append("- Cost is 0 everywhere: evals/pricing.json has no prices set.")
    return "\n".join(lines + ["", *notes])


def load_saved(paths: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for p in paths:
        for r in json.loads(Path(p).read_text(encoding="utf-8")):
            out[r["config"]].append(r)
    return dict(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--split", default="dev")
    r.add_argument("--repeats", type=int, default=3)
    r.add_argument("--configs", default="ABCDE")
    r.add_argument("--no-judges", action="store_true")
    t = sub.add_parser("table")
    t.add_argument("files", nargs="+")
    a = ap.parse_args()
    if a.cmd == "run":
        saved = []
        for c in a.configs:
            rows = asyncio.run(run_local(c, load_records(a.split), a.repeats, not a.no_judges))
            saved.append(str(save(c, a.split, rows)))
            print(f"config {c} done -> {saved[-1]}")
        print(build_table(load_saved(saved)))
    else:
        files = [f for pat in a.files for f in glob.glob(pat)]
        print(build_table(load_saved(files)))


if __name__ == "__main__":
    main()
