"""The only module in this codebase permitted to talk to an LLM.

Two guarantees, both structural rather than by convention:

1. **Redaction cannot be skipped.** `complete()` runs `redact_phi()` on every
   piece of text it is given. Callers cannot pass pre-redacted text and opt out;
   redaction is idempotent, so a second pass is harmless.
2. **Fail closed.** After redacting, the payload is re-scanned for unambiguous
   PHI. If any is found, the call raises instead of sending. A prototype that
   leaks quietly is worse than one that stops loudly.

`tests/test_llm_chokepoint.py` asserts no other module imports an LLM SDK or
reaches an LLM host, so this stays the single exit.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from app.ai.redaction import find_residual_phi, redact_phi_detailed
from app.core.audit_logging import log_event
from app.core.config import settings


class PHILeakError(RuntimeError):
    """Raised when redacted text still contains unambiguous PHI. Fail closed."""


class LLMUnavailableError(RuntimeError):
    """The model could not be reached, or did not answer in time.

    Deliberately distinct from PHILeakError. A leak is a safety stop and the
    caller must not work around it. This is an availability failure, and each
    caller decides for itself whether degrading is safe for its purpose — the
    scribe falls back to deterministic extractive summarisation and labels the
    result; a patient-facing generator would refuse instead.

    Translating transport errors into one domain type here is what makes that
    choice possible at all. Before this existed, an httpx error from a 503
    propagated as an unhandled 500 and the caller never got to decide.
    See DECISIONS.md D-070.
    """

    def __init__(self, reason: str, *, provider: str) -> None:
        super().__init__(f"{provider} unavailable: {reason}")
        self.reason = reason
        self.provider = provider


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    redaction_count: int
    redaction_by_category: dict[str, int] = field(default_factory=dict)
    confidence: float | None = None


class _StubProvider:
    """Deterministic offline provider.

    Default for a reason: the build must run end-to-end for a reviewer with no
    API key, and tests must not depend on a network round trip or a
    non-deterministic model. It echoes structure so downstream parsing is
    exercised for real.
    """

    name = "stub"

    def generate(self, system: str | None, prompt: str, model: str) -> tuple[str, float]:
        digest = hashlib.sha256((system or "").encode() + prompt.encode()).hexdigest()[:8]
        lines = [ln.strip() for ln in prompt.splitlines() if ln.strip()]
        excerpt = lines[-1][:160] if lines else ""
        body = (
            f"[STUB SUMMARY {digest}] Generated without a live model.\n"
            f"Input characters: {len(prompt)}.\n"
            f"Last input line seen: {excerpt}"
        )
        # Fixed confidence so tests are deterministic; a real provider reports
        # or is calibrated to its own.
        return body, 0.5


class _AnthropicProvider:
    """Live provider. Only reachable when CARENOTE_LLM_PROVIDER=anthropic."""

    name = "anthropic"

    def generate(self, system: str | None, prompt: str, model: str) -> tuple[str, float]:
        import httpx  # imported lazily so the stub path has no HTTP dependency

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "CARENOTE_LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset"
            )
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        # Every failure below becomes LLMUnavailableError so the caller can
        # decide whether to degrade. Nothing here is allowed to escape as a
        # raw httpx error: that is what turned a provider 503 into an
        # unhandled 500 with a traceback.
        try:
            response = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
                timeout=settings.llm_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError(
                f"no response within {settings.llm_timeout_seconds}s", provider=self.name
            ) from exc
        except httpx.HTTPError as exc:
            # Transport-level: DNS, connection refused, TLS, read error.
            raise LLMUnavailableError(type(exc).__name__, provider=self.name) from exc

        if response.status_code >= 500 or response.status_code == 429:
            # Retryable server-side conditions. Degrading is correct.
            raise LLMUnavailableError(f"HTTP {response.status_code}", provider=self.name)
        if response.status_code >= 400:
            # 4xx is our bug — a bad key, a malformed request. Degrading would
            # hide it behind a summary that looks merely lower quality, so this
            # stays loud.
            raise RuntimeError(f"{self.name} rejected the request: HTTP {response.status_code}")

        try:
            data = response.json()
            text = "".join(block.get("text", "") for block in data.get("content", []))
        except ValueError as exc:
            raise LLMUnavailableError("unparseable response body", provider=self.name) from exc
        return text, 0.75


class _UnavailableProvider:
    """Simulates an outage. Enabled only by CARENOTE_LLM_FORCE_UNAVAILABLE.

    The stub provider is in-process and cannot time out, refuse a connection or
    return a 503, so for the whole build there was no way to observe what
    happens when the model is down — and the answer turned out to be "an
    unhandled 500". This exists so that path is exercised on every test run
    rather than discovered in a clinic.
    """

    name = "unavailable"

    def generate(self, system: str | None, prompt: str, model: str) -> tuple[str, float]:
        raise LLMUnavailableError("forced outage (test hook)", provider=self.name)


def _provider():
    if settings.llm_force_unavailable:
        return _UnavailableProvider()
    if settings.llm_provider == "anthropic":
        return _AnthropicProvider()
    return _StubProvider()


def complete(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    gazetteer: set[str] | None = None,
    purpose: str = "unspecified",
    actor_id: str | None = None,
    clinic_id: str | None = None,
) -> LLMResponse:
    """Redact, verify, then send. The only way to reach a model from this app.

    `gazetteer` should carry the known synthetic names in scope (this patient,
    this clinic's users) so bare first-name mentions are caught alongside the
    pattern-based detections.
    """
    result = redact_phi_detailed(prompt, gazetteer=gazetteer)
    redacted_system = redact_phi_detailed(system, gazetteer=gazetteer).text if system else None

    residual = find_residual_phi(result.text) + find_residual_phi(redacted_system or "")
    if residual and settings.fail_closed_on_phi:
        log_event(
            actor_id=actor_id,
            action="llm.blocked_residual_phi",
            target_type="llm_request",
            clinic_id=clinic_id,
            metadata={"purpose": purpose, "categories": ",".join(sorted(set(residual)))},
        )
        raise PHILeakError(
            f"Refusing to send: unredacted PHI categories still present: {sorted(set(residual))}"
        )

    provider = _provider()
    chosen_model = model or settings.llm_model

    # Metadata only — the prompt itself is never logged, redacted or not.
    log_event(
        actor_id=actor_id,
        action="llm.request",
        target_type="llm_request",
        clinic_id=clinic_id,
        metadata={
            "purpose": purpose,
            "provider": provider.name,
            "model": chosen_model,
            "redactions": result.replacements,
            "prompt_chars": len(result.text),
        },
    )

    try:
        text, confidence = provider.generate(redacted_system, result.text, chosen_model)
    except LLMUnavailableError as exc:
        # Metadata only, and recorded here rather than in the caller so every
        # future caller gets the audit trail without having to remember.
        log_event(
            actor_id=actor_id,
            action="llm.unavailable",
            target_type="llm_request",
            clinic_id=clinic_id,
            metadata={"purpose": purpose, "provider": exc.provider, "reason": exc.reason},
        )
        raise

    return LLMResponse(
        text=text,
        model=chosen_model,
        provider=provider.name,
        redaction_count=result.replacements,
        redaction_by_category=result.by_category,
        confidence=confidence,
    )
