# 3. The guard and execute have no model

Status: accepted. Authority limit (R7), order-total cap, risk, confidence floor, unresolved critique and unknown evidence are checked in `guards.py`, which is pure and unit-tested. `risk is None` escalates. Execute takes the amount and order id from validated state and writes through a UNIQUE idempotency key. Rejected: asking the model to respect limits; a prompt is not a control.
