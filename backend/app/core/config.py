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
    # 60 minutes. Short enough that a stolen token has a bounded lifetime;
    # long enough to survive a consult without re-login. There is deliberately
    # no refresh flow — see DECISIONS.md D-016.
    jwt_ttl_minutes: int = int(os.getenv("CARENOTE_JWT_TTL_MINUTES", "60"))
    db_url: str = os.getenv("CARENOTE_DB_URL", "sqlite:///./carenote.db")

    # Browser sessions carry the token in an httpOnly cookie so that an XSS
    # payload cannot read it (DECISIONS.md D-016). Secure=True requires HTTPS,
    # so it is off for localhost dev and MUST be on in production.
    cookie_name: str = "carenote_access"
    cookie_secure: bool = os.getenv("CARENOTE_COOKIE_SECURE", "false").lower() == "true"
    cookie_samesite: str = "lax"

    # "stub" keeps the whole build runnable offline and deterministic.
    llm_provider: str = os.getenv("CARENOTE_LLM_PROVIDER", "stub")
    llm_model: str = os.getenv("CARENOTE_LLM_MODEL", "claude-sonnet-4-5")

    # Hard safety switch: if True, llm_client refuses to send text that still
    # matches a PHI pattern after redaction, rather than sending it anyway.
    fail_closed_on_phi: bool = True


settings = Settings()
