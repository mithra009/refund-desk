# 4. The risk assessment is a separate service in another framework

Status: accepted. `risk-service` (Google ADK, A2A, own container, own tests, own agent card) models a specialist owned by another team. The main graph knows only the contract (`level`, `signals`, `score`), duplicated on both sides and kept in step by a test. Timeout 5 s, 2 retries with jitter, circuit breaker; failure yields `risk=None`, which escalates. Rejected: building it in LangGraph too. You could; the boundary is the point being demonstrated.
