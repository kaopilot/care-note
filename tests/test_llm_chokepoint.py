"""The redaction boundary is only worth anything if it cannot be walked around.

Two kinds of check here:
  1. Behavioural — `complete()` redacts, and refuses to send if PHI survives.
  2. Structural — a source scan asserting no other module reaches an LLM.

The second is the one that keeps holding as the codebase grows in later phases.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.ai import llm_client
from app.ai.llm_client import PHILeakError, complete

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
CHOKEPOINT = BACKEND_DIR / "app" / "ai" / "llm_client.py"


# --------------------------------------------------------------------------
# Behaviour
# --------------------------------------------------------------------------


def test_complete_redacts_before_sending(monkeypatch) -> None:
    """Capture what the provider actually receives and assert the PHI is gone."""
    captured: dict[str, str] = {}

    class Spy:
        name = "spy"

        def generate(self, system, prompt, model):
            captured["prompt"] = prompt
            captured["system"] = system or ""
            return "ok", 0.5

    monkeypatch.setattr(llm_client, "_provider", lambda: Spy())

    complete(
        "Dr Lim reviewed Mdm Amira Rahman (NRIC S1234567D, tel +65 9123 4567).",
        system="Summarise the consult for Patient: Amira Rahman.",
        purpose="test",
    )

    sent = captured["prompt"] + captured["system"]
    assert "S1234567D" not in sent
    assert "9123 4567" not in sent
    assert "Amira Rahman" not in sent
    assert "Lim" not in sent
    # ...and the clinical substance survived the trip.
    assert "reviewed" in captured["prompt"]


def test_complete_reports_redaction_count(monkeypatch) -> None:
    monkeypatch.setattr(
        llm_client,
        "_provider",
        lambda: type("P", (), {"name": "spy", "generate": lambda s, a, b, c: ("ok", 0.5)})(),
    )
    response = complete("NRIC S1234567D and tel +65 9123 4567", purpose="test")
    assert response.redaction_count >= 2
    assert response.redaction_by_category["nric"] == 1


def test_fail_closed_when_phi_survives(monkeypatch) -> None:
    """If redaction is ever broken, the call must stop rather than leak."""
    monkeypatch.setattr(
        llm_client, "redact_phi_detailed",
        lambda text, gazetteer=None: type(
            "R", (), {"text": text, "replacements": 0, "by_category": {}}
        )(),
    )
    with pytest.raises(PHILeakError):
        complete("NRIC S1234567D must not escape", purpose="test")


def test_stub_provider_is_the_default() -> None:
    """The repo must run end-to-end with no API key and no network."""
    response = complete("Summarise: patient reports a cough.", purpose="test")
    assert response.provider == "stub"
    assert response.text


def test_no_prompt_content_in_logs(caplog) -> None:
    """Logging hygiene: the prompt is never written to a log line, redacted or
    not."""
    import logging

    caplog.set_level(logging.INFO)
    complete("Distinctive marker phrase zebracrossing.", purpose="test")
    assert "zebracrossing" not in caplog.text
    assert "llm.request" in caplog.text


# --------------------------------------------------------------------------
# Structure — nothing else may reach a model
# --------------------------------------------------------------------------


def _python_sources() -> list[Path]:
    return [
        path
        for path in BACKEND_DIR.rglob("*.py")
        if "__pycache__" not in path.parts and path != CHOKEPOINT
    ]


def test_no_other_module_imports_an_llm_sdk() -> None:
    banned = re.compile(r"^\s*(?:import|from)\s+(anthropic|openai|cohere|google\.generativeai)\b",
                        re.MULTILINE)
    offenders = [
        str(path.relative_to(BACKEND_DIR))
        for path in _python_sources()
        if banned.search(path.read_text())
    ]
    assert not offenders, f"LLM SDK imported outside llm_client.py: {offenders}"


def test_no_other_module_reaches_an_llm_host() -> None:
    hosts = re.compile(r"https?://[^\s\"']*(?:api\.anthropic\.com|api\.openai\.com)")
    offenders = [
        str(path.relative_to(BACKEND_DIR))
        for path in _python_sources()
        if hosts.search(path.read_text())
    ]
    assert not offenders, f"LLM endpoint referenced outside llm_client.py: {offenders}"


def test_chokepoint_still_calls_redaction() -> None:
    """Guards against a future edit quietly removing the redaction call."""
    source = CHOKEPOINT.read_text()
    assert "redact_phi_detailed(" in source
    assert "find_residual_phi(" in source
