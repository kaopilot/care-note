"""Regeneration safety and dosage confirmation.

Two capabilities from the reviewers' second-round list:

* *"AI regeneration that preserves human-confirmed and completed state"*
* *"Medical terminology and dosage confirmation — should what was captured be
  confirmed through medical references and human confirmation?"*

Both were DOES NOT before this. Regeneration was undefined behaviour: a new
session id produced a duplicate summary entry, and reusing the session id
crashed on the `transcript_segments` unique constraint. Dosages were compared
against each other (D-068) and never against a reference, so the build could
tell that two entries disagreed about a metformin dose and could not tell that
one of them said 5000mg.

See DECISIONS.md D-078 and D-079.
"""

from __future__ import annotations

import pytest

from app.core.enums import EntryType, HighlightStatus, InteractionType, Role
from app.models import AIScribedNote, Entry, Highlight, Patient, User, Version
from app.services import dosage, scribe


def _clinic_a(db):
    patient = db.query(Patient).filter(Patient.id == "patient-a1").one()
    clinician = db.query(User).filter(User.id == "u-a-clinician").one()
    return patient, clinician


def _generate(db, patient, clinician, **kwargs):
    return scribe.run_scribe(
        db,
        patient=patient,
        interaction_type=InteractionType.DOCTOR_PATIENT_CONSULT,
        actor_id=clinician.id,
        **kwargs,
    )


def _session_of(db, entry):
    return db.query(AIScribedNote).filter(AIScribedNote.entry_id == entry.id).one().session_id


# --- regeneration ---------------------------------------------------------


def test_rerunning_a_session_without_asking_is_refused(db_session, seeded):
    """Silently producing a second summary for one consult is not an answer."""
    patient, clinician = _clinic_a(db_session)
    first = _generate(db_session, patient, clinician)

    with pytest.raises(scribe.RegenerationRefused) as exc:
        _generate(db_session, patient, clinician, session_id=_session_of(db_session, first))
    assert exc.value.reason == "exists"


def test_regeneration_reuses_the_entry_rather_than_duplicating_it(db_session, seeded):
    """The entry id is what highlights, comments, tasks and pointers anchor to."""
    patient, clinician = _clinic_a(db_session)
    first = _generate(db_session, patient, clinician)
    session = _session_of(db_session, first)

    second = _generate(
        db_session, patient, clinician, session_id=session, regenerate=True
    )

    assert second.id == first.id
    assert second.version_number == 2
    assert (
        db_session.query(Entry)
        .filter(Entry.patient_id == patient.id, Entry.author_role == Role.SYSTEM)
        .count()
        == 1
    )


def test_regeneration_preserves_an_accepted_highlight(db_session, seeded):
    """The capability, stated literally."""
    patient, clinician = _clinic_a(db_session)
    first = _generate(db_session, patient, clinician)
    highlight = db_session.query(Highlight).filter(Highlight.entry_id == first.id).first()
    assert highlight is not None, "fixture produced no highlight to confirm"
    highlight.status = HighlightStatus.ACCEPTED
    db_session.commit()
    highlight_id = highlight.id

    _generate(
        db_session,
        patient,
        clinician,
        session_id=_session_of(db_session, first),
        regenerate=True,
    )

    survivor = db_session.query(Highlight).filter(Highlight.id == highlight_id).one_or_none()
    assert survivor is not None
    assert survivor.status == HighlightStatus.ACCEPTED


def test_regeneration_keeps_the_previous_summary_as_a_version(db_session, seeded):
    """Never destroys history — the old summary stays revertible."""
    patient, clinician = _clinic_a(db_session)
    first = _generate(db_session, patient, clinician)
    original = first.content

    _generate(
        db_session,
        patient,
        clinician,
        session_id=_session_of(db_session, first),
        regenerate=True,
    )

    snapshots = [
        v.content_snapshot
        for v in db_session.query(Version).filter(Version.entry_id == first.id).all()
    ]
    assert original in snapshots


def test_regeneration_refuses_to_overwrite_a_clinician_edit(db_session, seeded):
    """The expensive reading of the capability, and the one that matters.

    Keeping accepted highlights is cheap. Not replacing a clinician's own words
    with a model's second attempt is the real requirement — and merging would
    mean deciding which of their sentences to keep, which is a clinical
    judgement the system has no standing to make (D-068).
    """
    patient, clinician = _clinic_a(db_session)
    first = _generate(db_session, patient, clinician)
    session = _session_of(db_session, first)

    db_session.add(
        Version(
            entry_id=first.id,
            version_number=2,
            content_snapshot="Clinician's own wording.",
            edited_by=clinician.id,
            edited_by_role=Role.CLINICIAN,
        )
    )
    first.version_number = 2
    first.content = "Clinician's own wording."
    db_session.commit()

    with pytest.raises(scribe.RegenerationRefused) as exc:
        _generate(db_session, patient, clinician, session_id=session, regenerate=True)
    assert exc.value.reason == "human_edited"

    preserved = db_session.query(Entry).filter(Entry.id == first.id).one()
    assert preserved.content == "Clinician's own wording."


def test_the_refusal_tells_the_clinician_what_to_do_next(db_session, seeded):
    """Refusing is only acceptable because it is recoverable."""
    patient, clinician = _clinic_a(db_session)
    first = _generate(db_session, patient, clinician)
    db_session.add(
        Version(
            entry_id=first.id,
            version_number=2,
            content_snapshot="edited",
            edited_by=clinician.id,
            edited_by_role=Role.CLINICIAN,
        )
    )
    first.version_number = 2
    db_session.commit()

    with pytest.raises(scribe.RegenerationRefused) as exc:
        _generate(
            db_session,
            patient,
            clinician,
            session_id=_session_of(db_session, first),
            regenerate=True,
        )
    assert "revert" in str(exc.value).lower()


# --- dosage plausibility --------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Continue metformin 500mg BD.", []),
        ("Warfarin 5mg daily, INR stable.", []),
        ("Levothyroxine 100mcg daily.", []),
        ("Insulin 12 units nocte.", []),  # units, not mass — nothing to compare
        ("Started on metformin 1500mg BD.", [dosage.UNUSUAL]),
        ("Take metformin 5000mg daily.", [dosage.IMPLAUSIBLE]),
        ("Warfarin 500mg daily.", [dosage.IMPLAUSIBLE]),
    ],
)
def test_dose_plausibility_bands(text, expected):
    assert [f.state for f in dosage.check_text(text)] == expected


def test_a_legitimate_high_dose_is_unusual_not_blocking():
    """1500mg metformin BD is real prescribing. Gating on it teaches reflex clicking."""
    findings = dosage.check_text("Metformin 1500mg BD.")
    assert findings and not findings[0].needs_human_confirmation


def test_the_decimal_slip_blocks():
    findings = dosage.blocking_findings("Take metformin 5000mg daily.")
    assert len(findings) == 1
    assert "5000mg" in findings[0].message


def test_a_dose_far_from_its_drug_is_not_attributed_to_it():
    """A dose three sentences away belongs to something else."""
    text = "Discussed warfarin at length. " + ("Filler sentence. " * 6) + "Aspirin 300mg."
    assert all(f.drug != "warfarin" for f in dosage.check_text(text))


# --- the human gate on patient-facing content -----------------------------


def _login(client, username):
    response = client.post("/auth/login", json={"username": username, "password": "pw"})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _instruction(content, **extra):
    return {
        "type": str(EntryType.PATIENT_INSTRUCTION),
        "title": "Your next steps",
        "content": content,
        **extra,
    }


def test_an_implausible_dose_to_a_patient_is_refused_until_confirmed(client, seeded):
    clinician = _login(client, "clinician_a")
    response = client.post(
        "/patients/patient-a1/entries",
        headers=clinician,
        json=_instruction("Take metformin 5000mg once daily."),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "dosage_needs_confirmation"


def test_confirming_lets_it_through(client, seeded):
    """Acknowledgement, not refusal. A specialist regimen must still be recordable."""
    clinician = _login(client, "clinician_a")
    response = client.post(
        "/patients/patient-a1/entries",
        headers=clinician,
        json=_instruction("Take metformin 5000mg once daily.", dosage_confirmed=True),
    )
    assert response.status_code == 201


def test_an_ordinary_dose_needs_no_confirmation(client, seeded):
    clinician = _login(client, "clinician_a")
    response = client.post(
        "/patients/patient-a1/entries",
        headers=clinician,
        json=_instruction("Take metformin 500mg twice daily."),
    )
    assert response.status_code == 201


def test_internal_notes_are_not_gated(client, seeded):
    """Internal notes get audited. The gate is for what leaves the building."""
    clinician = _login(client, "clinician_a")
    response = client.post(
        "/patients/patient-a1/entries",
        headers=clinician,
        json={
            "type": str(EntryType.CLINICIAN_SECTION),
            "title": "Plan",
            "content": "Consider metformin 5000mg — check against BNF.",
        },
    )
    assert response.status_code == 201
