"""All tunables in one place: model tiers, limits, thresholds, ablation switches.

Nothing in a node hard-codes a model name or a threshold; it reads `cfg`.
Environment variables override (prefix REFUND_DESK_, or the bare names below).
"""
from __future__ import annotations

from datetime import datetime

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend_sim.factsheet import AS_OF


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="REFUND_DESK_", extra="ignore",
                                      populate_by_name=True)

    # ---- model tiers (init_chat_model identifiers) ----------------------
    fast: str = "google_genai:gemini-3.5-flash-lite"    # intake, workers, reply
    strong: str = "google_genai:gemini-3.1-flash-lite"    # lead plan, decision, critic
    fast_fallback: str = "google_genai:gemini-3.1-flash-lite"
    strong_fallback: str = "google_genai:gemini-3.5-flash-lite"
    critic_model: str | None = None                # None = strong; set to another family for the ablation
    model_timeout_s: float = 60.0
    requests_per_minute: int | None = 12            # per model id, client-side; free tier is 15. None = unthrottled
    temperature: float | None = 0.0                 # None = provider default (for models that reject the parameter)

    # ---- infrastructure ---------------------------------------------------
    database_url: str = Field("sqlite:///:memory:", validation_alias=AliasChoices("DATABASE_URL"))
    checkpoint_dsn: str | None = Field(None, validation_alias=AliasChoices("CHECKPOINT_DSN"))
    risk_service_url: str = "http://localhost:8001"
    as_of: str | None = Field(None, validation_alias=AliasChoices("REFUND_DESK_AS_OF"))

    # ---- business thresholds (R7, R9, guard) -----------------------------
    authority_cents: int = 10_000
    conf_floor: float = 0.6
    evidence_conf_floor: float = 0.3

    # ---- loop and budget caps ---------------------------------------------
    max_plan_items: int = 5
    max_worker_steps: int = 6
    max_revisions: int = 2
    max_tokens_per_case: int = 150_000
    max_tool_calls_per_case: int = 40
    max_wall_clock_s: float = 180.0
    recursion_limit: int = 60

    # ---- risk client --------------------------------------------------------
    risk_timeout_s: float = 90.0                    # the risk agents make several model calls; 5s suited a stub, not Gemini
    risk_retries: int = 1
    breaker_failures: int = 3
    breaker_reset_s: float = 30.0

    # ---- ablation switches (section 12) -------------------------------------
    critic_enabled: bool = True
    hierarchy_enabled: bool = True          # False = one investigator with all read tools (config D)

    # ---- observability -------------------------------------------------------
    prompt_version: str = "local"
    git_sha: str = "dev"
    langsmith_tracing: bool = False

    def now(self) -> datetime:
        return datetime.fromisoformat(self.as_of) if self.as_of else AS_OF

    @property
    def critic_model_id(self) -> str:
        return self.critic_model or self.strong


cfg = Config()
