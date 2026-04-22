# Architecture notes

How the code maps to IMPLEMENTATION.md, and where it deliberately differs.

## Map

| Design section | Code |
|---|---|
| 5.1 fake backend | `backend_sim/schema.sql`, `seed.py`, `payments.py`, `db.py` (Postgres, or SQLite for tests) |
| 5.2 policy | `data/policy/*.md` (25 clauses, R1–R25) |
| 5.3 / 6 contracts and state | `services/refund_desk/app/state.py` |
| 7.1 intake | `nodes/intake.py` |
| 7.2 supervisor | `nodes/supervisor.py` |
| 7.3 lead, merge | `nodes/lead.py`, `nodes/merge.py` |
| 7.4 workers | `workers/shipment.py` (hand-built), `workers/prebuilt.py` (+ `order.py`, `history.py`), `tools/backend.py` |
| 7.5 retrieval | `retrieval.py` |
| 7.6 draft / critic | `nodes/decide.py`, `nodes/critic.py` |
| 7.7 guard | `guards.py` (pure functions) + `nodes/guard.py` (thin wrapper) |
| 7.8 / 7.9 / 7.10 | `nodes/human.py`, `nodes/execute.py`, `nodes/reply.py` |
| 8 risk service | `services/risk_service/{agent,tools,serve,contract}.py`; client `app/risk_client.py` |
| 9 production | `config.py` (limits), `redact.py`, `observability.py`, `memory.py`, `api.py`, `audit.py` |
| 10 / 11 / 12 | `evals/` (`oracle`, `generate`, `evaluators`, `run`, `ablation`, `gate`, `smoke`, `calibrate`) |

## Deviations from the design document, and why

| Design | Here | Reason |
|---|---|---|
| `plan: list[str]` | `plan: list[PlanItem]` | The merge step must know which worker to re-dispatch an unanswered question to. |
| Tool `customer_id` "from injected state" | Bound into each tool set when it is built (`build_tools(customer_id)`) | Same guarantee (the model cannot supply it) without depending on `InjectedState` across versions. |
| Supervisor routes to `decide` | Node is named `draft` | Matches the diagram's DRAFT node. |
| `evidence` merge "drops findings under a floor" | Done by a view (`usable_evidence`), not by mutating state | `evidence` has an append-only reducer; dropping is a read-time filter. |
| Budget "tracked in state" | `budget` + `spent` (reducer) folded by the merge step | Parallel workers cannot all write `budget`; one node folds their spend. |
| A2A client "classes" | Async `a2a.client.create_client`, protobuf `SendMessageRequest` | The a2a-sdk 1.x API; the risk_check node is async. |
| Tables listed in 5.1 | + `carrier_claims`, `policy_chunks`; `customers` gains `payment_changes_30d`, `address_reuse_accounts`; `order_items.note` | Needed by `get_carrier_claim`, pgvector retrieval, the risk signals, and tool-output injection tests. |
| Oracle for R7 | Over-authority cases have outcome `approve_*` with `requires_human=true` | R7 limits who executes, not what is right (policy R11). `unsafe_action` treats executing such a case as unsafe. |
| Config A "single ReAct" | Includes a `list_recent_orders` tool and a photo flag appended to the message | Without an intake step it has no other way to find the order or learn about the upload. |

## State and flow

- `evidence` and `critiques` use `operator.add`; `spent` does too. Everything else is last-write-wins.
- Workers receive a `WorkerInput` (question, order id, customer id, worker, index) and return `{"evidence": [...], "spent": [...]}`. Message histories stay inside the worker.
- `human_review` has no side effect before `interrupt()`. `execute` is a separate node with no retry policy.
- Thread id = case id. The API drives the graph with `astream(stream_mode="updates")` and fans node names out over SSE.

## Failure behaviour

| Failure | Result |
|---|---|
| Risk service down / slow / breaker open | `risk=None` -> guard reason `risk` -> human review |
| A worker raises | Zero-confidence evidence -> one re-dispatch -> `unknown` evidence -> guard reason `unknown_evidence` |
| Model returns unparseable output | `ValueError` -> node `RetryPolicy` (3 attempts); provider errors fall back to the next model tier |
| Token / tool-call / wall-clock budget exceeded | Supervisor routes to human review with what was gathered |
| Reply leaks a clause id / risk term / wrong amount | One retry with feedback, then a fixed template |
| Process killed during review | Resume from the checkpoint; idempotency key prevents a second refund |
