"""Run one configuration over a dataset split and score it.

    python -m evals.run --config E --split dev --repeats 3            # local, writes evals/results/
    python -m evals.run --config E --split dev --langsmith            # also a LangSmith experiment
    python -m evals.run --config E --split dev --limit 10 --no-judges # pilot

Configs: A single ReAct, B pipeline, C full minus critic, D full minus hierarchy, E full.
Interrupts are auto-resolved as "escalated": the run stops at the human gate, nothing is approved.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import time
import uuid
from collections import defaultdict
from pathlib import Path

from app.config import cfg
from backend_sim.db import get_db
from backend_sim.factsheet import FactSheet
from backend_sim.seed import seed_fact_sheet
from evals import evaluators as ev

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "data" / "scenarios" / "scenarios.jsonl"
RESULTS = Path(__file__).with_name("results")
CONFIGS = {"A": "single ReAct", "B": "pipeline", "C": "full minus critic", "D": "full minus hierarchy", "E": "full"}


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=ROOT, timeout=5).stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def price_per_mtok(config: str) -> float:
    p = json.loads(Path(__file__).with_name("pricing.json").read_text(encoding="utf-8"))
    return float(p.get(config, p.get("default", 0.0)))


def load_records(split: str | None = None, path: Path = SCENARIOS) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if split in (None, "all", r["split"])]


def outputs_from_state(state: dict, latency_s: float, config: str) -> dict:
    """Flatten a graph result into the shape the evaluators read."""
    interrupted = "__interrupt__" in state
    final, draft = state.get("final"), state.get("draft")
    decision = final or draft
    budget = state.get("budget")
    evidence = state.get("evidence", [])
    tools = [t["tool"] for e in evidence for t in e.get("raw", {}).get("tool_results", [])]
    executed = bool(state.get("refund_id"))
    status = ("executed" if executed else "escalated_early" if interrupted and draft is None
              else "escalated" if interrupted else "denied" if decision and decision.outcome == "deny" else "closed")
    tokens = budget.tokens if budget else 0
    return {
        "decision": decision.model_dump() if decision else None,
        "status": status, "executed": executed,
        "executed_amount_cents": final.amount_cents if executed and final else None,
        "clauses_retrieved": [c.id for c in state["policy_hits"]] if state.get("policy_hits") else None,
        "evidence": [{"id": e["id"], "finding": e["finding"]} for e in evidence],
        "tools_called": tools, "plan": [p.question for p in state.get("plan", [])],
        "guard_reasons": state["guard"].reasons if state.get("guard") else [],
        "risk_level": state["risk"].level if state.get("risk") else None,
        "steps": (budget.steps + budget.tool_calls) if budget else 0,
        "reply": state.get("reply"), "tokens": tokens, "latency_s": round(latency_s, 3),
        "cost_usd": round(tokens / 1e6 * price_per_mtok(config), 6),
    }


def outputs_from_baseline(res: dict, latency_s: float, config: str) -> dict:
    d = res["decision"]
    return {
        "decision": d.model_dump(), "status": "executed" if res["executed"] else "denied" if d.outcome == "deny" else "closed",
        "executed": res["executed"], "executed_amount_cents": d.amount_cents if res["executed"] else None,
        "clauses_retrieved": None,
        "evidence": [{"id": r["tool"], "finding": json.dumps(r["result"], default=str)[:400]} for r in res["tool_results"]],
        "tools_called": [r["tool"] for r in res["tool_results"]], "plan": [],
        "steps": res["tool_calls"] + 1, "reply": None, "tokens": res["tokens"],
        "latency_s": round(latency_s, 3), "cost_usd": round(res["tokens"] / 1e6 * price_per_mtok(config), 6),
    }


class Runner:
    def __init__(self, config: str):
        assert config in CONFIGS, config
        self.config = config
        cfg.critic_enabled = config != "C"
        cfg.hierarchy_enabled = config != "D"
        self.graph = None
        if config != "A":
            from langgraph.checkpoint.memory import InMemorySaver
            from app.graph import build_graph
            self.graph = build_graph(checkpointer=InMemorySaver(), variant="pipeline" if config == "B" else "full")

    async def run_case(self, inputs: dict) -> dict:
        """The evaluation target. Seeds the backend from the hidden facts; the agent gets only the message."""
        facts = FactSheet.model_validate(inputs["facts"])
        seed_fact_sheet(get_db(), facts)
        case_id = f"eval-{facts.scenario_id}-{self.config}-{uuid.uuid4().hex[:8]}"
        t0 = time.perf_counter()
        try:
            if self.config == "A":
                from app.variants import run_baseline
                res = await asyncio.to_thread(run_baseline, case_id, inputs["customer_id"], inputs["message"],
                                              bool(inputs.get("has_photo")))
                return outputs_from_baseline(res, time.perf_counter() - t0, self.config)
            from app.observability import run_tags
            from app.state import Budget
            payload = {"case_id": case_id, "customer_id": inputs["customer_id"], "message": inputs["message"],
                       "has_photo": bool(inputs.get("has_photo")), "budget": Budget(), "evidence": [],
                       "critiques": [], "revision_count": 0, "policy_hits": [], "plan": [], "spent": []}
            state = await self.graph.ainvoke(payload, {"configurable": {"thread_id": case_id},
                                                       "recursion_limit": cfg.recursion_limit, **run_tags(case_id)})
            return outputs_from_state(state, time.perf_counter() - t0, self.config)
        except Exception as e:
            return {"decision": None, "status": "failed", "error": f"{type(e).__name__}: {e}"[:300], "executed": False,
                    "executed_amount_cents": None, "clauses_retrieved": None, "evidence": [], "tools_called": [],
                    "steps": 0, "reply": None, "tokens": 0, "latency_s": round(time.perf_counter() - t0, 3),
                    "cost_usd": 0.0}


def score(outputs: dict, reference: dict, judges: bool) -> dict:
    fns = ev.CODE_EVALUATORS + (ev.LLM_EVALUATORS if judges else [])
    scores = {}
    for fn in fns:
        try:
            scores[fn.__name__] = fn(outputs, reference)
        except Exception as e:      # a broken evaluator must not hide a result
            scores[fn.__name__] = f"error:{type(e).__name__}"
    return scores


async def run_local(config: str, records: list[dict], repeats: int, judges: bool, concurrency: int = 4) -> list[dict]:
    runner, sem = Runner(config), asyncio.Semaphore(concurrency)
    out: list[dict] = []

    async def one(rec: dict, rep: int):
        async with sem:
            o = await runner.run_case({"facts": rec["facts"], "message": rec["message"],
                                       "customer_id": rec["customer_id"], "has_photo": rec["has_photo"]})
            out.append({"id": rec["id"], "slice": rec["slice"], "rep": rep, "config": config, "outputs": o,
                        "scores": score(o, rec["reference"], judges)})

    await asyncio.gather(*(one(r, rep) for rep in range(repeats) for r in records))
    return sorted(out, key=lambda r: (r["rep"], r["id"]))


# ---- summaries ---------------------------------------------------------------------------------
def p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def summarize(rows: list[dict]) -> dict:
    n = len(rows) or 1
    sc = lambda k: [r["scores"][k] for r in rows if isinstance(r["scores"].get(k), (int, float))]
    return {
        "n": len(rows),
        "outcome_acc": sum(sc("outcome_correct")) / n,
        "mean_amount_error": sum(sc("amount_error_cents")) / n,
        "unsafe_actions": int(sum(sc("unsafe_action"))),
        "over_refunds": int(sum(sc("over_refund"))),
        "escalation_rate": sum(r["outputs"]["status"] in ("escalated", "escalated_early") for r in rows) / n,
        "failed": sum(r["outputs"]["status"] == "failed" for r in rows),
        "cost_per_case": sum(r["outputs"]["cost_usd"] for r in rows) / n,
        "p95_latency": p95([r["outputs"]["latency_s"] for r in rows]),
        "clauses_valid": (sum(sc("clauses_valid")) / max(1, len(sc("clauses_valid")))),
        "retrieval_recall_at_6": (statistics.mean(sc("retrieval_recall_at_6")) if sc("retrieval_recall_at_6") else None),
    }


def by_slice(rows: list[dict]) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[r["slice"]].append(r)
    return {s: summarize(g) for s, g in sorted(groups.items())}


def save(config: str, split: str, rows: list[dict]) -> Path:
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{config}-{split}-{git_sha()}-{int(time.time())}.json"
    path.write_text(json.dumps(rows, default=str), encoding="utf-8")
    return path


def run_langsmith(config: str, split: str, repeats: int, judges: bool, concurrency: int):
    from langsmith import Client
    runner = Runner(config)
    fns = ev.CODE_EVALUATORS + (ev.LLM_EVALUATORS if judges else [])
    return Client().evaluate(
        lambda inputs: asyncio.run(runner.run_case(inputs)),
        data=f"refund-desk-{split}", evaluators=[ev.as_langsmith(f) for f in fns],
        experiment_prefix=f"{config}-{git_sha()}", max_concurrency=concurrency, num_repetitions=repeats,
        metadata={"config": config, "git_sha": git_sha(), "prompt_version": cfg.prompt_version})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", choices=list(CONFIGS), default="E")
    ap.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--slice")
    ap.add_argument("--no-judges", action="store_true")
    ap.add_argument("--langsmith", action="store_true")
    ap.add_argument("--concurrency", type=int, default=4)
    a = ap.parse_args()
    if a.langsmith:
        run_langsmith(a.config, a.split, a.repeats, not a.no_judges, a.concurrency)
        return
    records = [r for r in load_records(a.split) if a.slice in (None, r["slice"])][: a.limit]
    rows = asyncio.run(run_local(a.config, records, a.repeats, not a.no_judges, a.concurrency))
    path = save(a.config, a.split, rows)
    print(json.dumps(summarize(rows), indent=1))
    print(json.dumps({k: round(v["outcome_acc"], 3) for k, v in by_slice(rows).items()}))
    print(f"saved {path}")


if __name__ == "__main__":
    main()
