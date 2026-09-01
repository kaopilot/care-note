"""What happens when the model is down, slow, or something crashes.

These cover three defects found by working through the clinic scenarios, all of
which shared one cause: the stub provider is in-process and cannot fail, so no
test run ever exercised a failure path.

1. A provider 503 propagated as an unhandled 500. The extractive summariser
   existed but only ran when the model returned unparseable JSON — degradation
   was wired to the wrong failure. (scenario 9)
2. Unhandled exceptions were logged by uvicorn with full tracebacks, and
   SQLAlchemy puts bound parameters in its exception messages. (scenario 3)
3. The timeout was 60s, which is not a timeout a clinician standing next to a
   patient can use. (scenario 8)
"""

from __future__ import annotations

import logging

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.ai import llm_client
from app.core.config import settings
from app.core.enums import InteractionType
from app.main import app
from app.models import AIScribedNote, Patient, User
from app.services import scribe


# --- 1. the model is down -------------------------------------------------


@pytest.fixture
def forced_outage(monkeypatch):
    """Force every LLM call to fail the way a real provider outage does."""
    monkeypatch.setattr(
        llm_client, "_provider", lambda: llm_client._UnavailableProvider()
    )


def test_provider_outage_raises_typed_error_not_transport_error(forced_outage):
    """The chokepoint translates transport failures into one domain type.

    This is what lets a caller decide whether degrading is safe. Before it
    existed, an httpx error escaped and the caller never got the choice.
    """
    with pytest.raises(llm_client.LLMUnavailableError) as exc:
        llm_client.complete("clinic visit notes", purpose="test")
    assert exc.value.provider == "unavailable"


def _clinic_a(db):
    patient = db.query(Patient).filter(Patient.id == "patient-a1").one()
    clinician = db.query(User).filter(User.id == "u-a-clinician").one()
    return patient, clinician


def test_scribe_still_produces_a_summary_when_the_model_is_down(
    db_session, seeded, forced_outage
):
    """The clinician loses fluency, not the consult."""
    patient, clinician = _clinic_a(db_session)
    entry = scribe.run_scribe(
        db_session,
        patient=patient,
        interaction_type=InteractionType.DOCTOR_PATIENT_CONSULT,
        actor_id=clinician.id,
    )

    assert entry is not None
    assert entry.content.strip(), "a degraded summary must still have content"


def test_degraded_summary_is_labelled_as_degraded(db_session, seeded, forced_outage):
    """A degraded note must be legible as degraded, not merely worse.

    An unlabelled fallback is arguably more dangerous than an error: the
    clinician reads a thinner summary and has no way to know the model never
    ran.
    """
    patient, clinician = _clinic_a(db_session)
    entry = scribe.run_scribe(
        db_session,
        patient=patient,
        interaction_type=InteractionType.DOCTOR_PATIENT_CONSULT,
        actor_id=clinician.id,
    )

    note = (
        db_session.query(AIScribedNote)
        .filter(AIScribedNote.entry_id == entry.id)
        .one()
    )
    assert note.model_used == "offline-extractive-v1:provider-unavailable"
    # Distinct from the no-model-configured case, so the two are separable in
    # an audit rather than collapsing into one label.
    assert note.model_used != "offline-extractive-v1"


def test_outage_is_recorded_in_the_audit_log(db_session, seeded, forced_outage, caplog):
    """An outage that leaves no trace cannot be investigated afterwards."""
    patient, clinician = _clinic_a(db_session)
    with caplog.at_level(logging.INFO):
        scribe.run_scribe(
            db_session,
            patient=patient,
            interaction_type=InteractionType.NURSE_PATIENT_CONSULT,
            actor_id=clinician.id,
        )
    assert any("llm.unavailable" in record.getMessage() for record in caplog.records)


# --- 2. logs must not carry patient data ----------------------------------


_LEAKY_ROUTE = APIRouter()


@_LEAKY_ROUTE.get("/test-only/crash")
def _crash():
    """Raise an exception whose message carries row data, as SQLAlchemy's does."""
    raise ValueError(
        "UNIQUE constraint failed [parameters: "
        "('e1', 2, 'Amira Rahman, NRIC S8412345D, allergic to penicillin')]"
    )


app.include_router(_LEAKY_ROUTE)


def test_crash_response_carries_no_patient_data():
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/test-only/crash")

    assert response.status_code == 500
    body = response.text
    for leaked in ("Amira", "S8412345D", "penicillin", "parameters"):
        assert leaked not in body, f"{leaked!r} reached the client"
    assert response.headers.get("X-Error-Reference"), "no reference to trace the report by"


def test_crash_log_carries_no_patient_data(caplog):
    """The important half. The response was never the leak — the log was."""
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.DEBUG):
        client.get("/test-only/crash")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for leaked in ("Amira", "S8412345D", "penicillin", "parameters"):
        assert leaked not in logged, f"{leaked!r} reached the application log"
    assert "ValueError" in logged, "the type must still be logged, or this is undebuggable"


def test_crash_log_records_enough_to_investigate(caplog):
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.DEBUG):
        response = client.get("/test-only/crash")

    ref = response.headers["X-Error-Reference"]
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert ref in logged, "the client's reference must appear in the log or it is useless"
    assert "/test-only/crash" in logged


# --- 3. the timeout is usable by a person waiting -------------------------


def test_timeout_is_short_enough_for_a_consult():
    """60s is a batch-job timeout. A clinician is standing next to a patient."""
    assert settings.llm_timeout_seconds <= 10, (
        "a summary that takes longer than this has already missed its consult"
    )


# --- Is the degradation actually visible to a clinician? -------------------
#
# Scenario 9 asks what the clinician gets during an outage. The answer was
# "a rule-derived summary, labelled" — true of the data and much weaker in the
# interface, where the only trace was `ai_model_used` rendered as a 10px grey
# monospace string in the provenance footer, beside the pointer. That is a
# machine-facing label in the place a clinician looks least. A degraded note
# that reads as a normal AI note is the failure this guards against.
#
# See DECISIONS.md D-082.


def test_degraded_state_is_a_first_class_field_not_a_magic_string(
    db_session, seeded, forced_outage
):
    """The API answers "was this degraded?" directly.

    A client that has to substring-match a model identifier will eventually
    match it differently from the next client.
    """
    from app.services import scribe

    assert scribe.is_degraded(scribe.DEGRADED_MODEL_LABEL) is True
    assert scribe.is_degraded("anthropic:claude-sonnet-4-5") is False
    assert scribe.is_degraded("offline-extractive-v1") is False, (
        "no model configured is not the same as the model being unreachable"
    )
    assert scribe.is_degraded(None) is False


def _wire(db, entry):
    """The entry as the client actually receives it."""
    from app.routes.schemas import entry_out

    note = (
        db.query(AIScribedNote).filter(AIScribedNote.entry_id == entry.id).one_or_none()
    )
    return entry_out(entry, ai_note=note)


def test_degraded_entry_reports_ai_degraded_on_the_wire(db_session, seeded, forced_outage):
    """The flag reaches the client that renders the card.

    The label existed before this and lived only in `ai_model_used`, rendered
    as a 10px grey monospace string in the provenance footer next to the
    pointer — a machine-facing label in the place a clinician looks least. A
    degraded note that reads like a normal AI note is the failure here.
    """
    patient, clinician = _clinic_a(db_session)
    entry = scribe.run_scribe(
        db_session,
        patient=patient,
        interaction_type=InteractionType.DOCTOR_PATIENT_CONSULT,
        actor_id=clinician.id,
    )

    out = _wire(db_session, entry)
    assert out.ai_degraded is True
    assert out.content.strip(), "still a usable summary, not an empty card"


def test_healthy_entry_is_not_marked_degraded(db_session, seeded):
    """The signal has to be able to be off, or it means nothing."""
    patient, clinician = _clinic_a(db_session)
    entry = scribe.run_scribe(
        db_session,
        patient=patient,
        interaction_type=InteractionType.NURSE_PATIENT_CONSULT,
        actor_id=clinician.id,
    )

    assert _wire(db_session, entry).ai_degraded is False


def test_degraded_is_independent_of_confidence(db_session, seeded, forced_outage):
    """Two different questions, two different signals.

    Collapsing them would tell a clinician the summary was uncertain when the
    real problem is that no model read the consult at all. They call for
    different responses, so they get different badges.
    """
    patient, clinician = _clinic_a(db_session)
    entry = scribe.run_scribe(
        db_session,
        patient=patient,
        interaction_type=InteractionType.AI_PATIENT_SESSION,
        actor_id=clinician.id,
    )

    out = _wire(db_session, entry)
    assert out.ai_degraded is True
    assert out.ai_confidence is not None, (
        "a degraded note still carries a confidence; the fields are orthogonal"
    )
