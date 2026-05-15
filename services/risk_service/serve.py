"""Expose the ADK risk agent over A2A.

    uvicorn risk_service.serve:app --port 8001

The agent card is served at /.well-known/agent-card.json.
"""
from __future__ import annotations

import os

from google.adk.a2a.utils.agent_to_a2a import to_a2a

from backend_sim.db import get_db
from risk_service.agent import root_agent

PORT = int(os.environ.get("RISK_PORT", "8001"))
HOST = os.environ.get("RISK_PUBLIC_HOST", "localhost")

get_db()      # fail fast if the database is unreachable
app = to_a2a(root_agent, host=HOST, port=PORT)
