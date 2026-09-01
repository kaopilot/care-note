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

    # A consult summary that takes longer than this has already missed its
    # consult. The previous value was 60s, which is not a timeout a clinician
    # standing next to a patient can use — it is a timeout for a batch job.
    # See DECISIONS.md D-070.
    llm_timeout_seconds: float = float(os.getenv("CARENOTE_LLM_TIMEOUT_SECONDS", "8"))

    # Test-only hook. Forces the provider to raise LLMUnavailableError so the
    # degraded path can be exercised without an actual outage. Never set in
    # normal operation; the stub provider cannot fail on its own, which is
    # exactly why the outage path went unnoticed until it was asked about.
    llm_force_unavailable: bool = (
        os.getenv("CARENOTE_LLM_FORCE_UNAVAILABLE", "false").lower() == "true"
    )

    # Hard safety switch: if True, llm_client refuses to send text that still
    # matches a PHI pattern after redaction, rather than sending it anyway.
    fail_closed_on_phi: bool = True

    # Demo affordance only. The scribe pipeline is synchronous, so with a
    # fast offline summariser the client's "processing" state would flash by
    # too quickly to see. Set to e.g. 1200 when recording the demo; 0
    # everywhere else, including tests.
    scribe_delay_ms: int = int(os.getenv("CARENOTE_SCRIBE_DELAY_MS", "0"))

    # --- Phase 5: ambient voice capture ---------------------------------
    # "stub" (default, in-process, simulated and flagged as such), "local"
    # (documented production path, unimplemented), or "remote".
    asr_provider: str = os.getenv("CARENOTE_ASR_PROVIDER", "stub")
    asr_model: str = os.getenv("CARENOTE_ASR_MODEL", "whisper-large-v3")

    # Audio cannot be redacted before transcription — there is no regex over a
    # waveform (see app/ai/asr_client.py). So sending a recording to a hosted
    # recogniser means sending un-redacted patient speech off-box, and that
    # requires someone to say so explicitly. Default off; the remote provider
    # raises rather than downgrading to the stub when it is off.
    asr_allow_audio_egress: bool = (
        os.getenv("CARENOTE_ASR_ALLOW_AUDIO_EGRESS", "false").lower() == "true"
    )


settings = Settings()
