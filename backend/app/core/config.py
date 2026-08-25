"""Central configuration. Read from environment; safe defaults for local dev.

Nothing here should ever hold real credentials in the repo — see .env.example.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    jwt_secret: str = os.getenv("CARENOTE_JWT_SECRET", "dev-only-insecure-secret-change-me")
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 12 * 60
    db_url: str = os.getenv("CARENOTE_DB_URL", "sqlite:///./carenote.db")

    # "stub" keeps the whole build runnable offline and deterministic.
    llm_provider: str = os.getenv("CARENOTE_LLM_PROVIDER", "stub")
    llm_model: str = os.getenv("CARENOTE_LLM_MODEL", "claude-sonnet-4-5")

    # Hard safety switch: if True, llm_client refuses to send text that still
    # matches a PHI pattern after redaction, rather than sending it anyway.
    fail_closed_on_phi: bool = True


settings = Settings()
