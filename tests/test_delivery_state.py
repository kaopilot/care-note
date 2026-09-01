"""Did anything written for the patient actually reach her?

Scenarios 11 and 12. This build has no sender, and these tests do not pretend
otherwise. What they pin is the honest half: the system must be able to say that
something was written and never read, and must tell a patient when the text in
front of her is not the text she read last time.

The original failure: a clinician writes an instruction, marks it done, and moves
on. The patient never opens the portal. The instruction is correct, versioned,
traceable and unread — and the system reports success.

See DECISIONS.md D-074.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.enums import EntryType, Role
from app.models import Entry, Patient, PatientView, User, Version
from app.services import delivery


def _utc(offset_minutes: int = 0) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=offset_minutes)


@pytest.fixture
def instruction(db_session, seeded):
    patient = db_session.query(Patient).filter(Patient.id == "patient-a1").one()
    entry = Entry(
        id="e-instruction",
        patient_id=patient.id,
        clinic_id=patient.clinic_id,
        author_id="u-a-clinician",
        author_role=Role.CLINICIAN,
        type=str(EntryType.PATIENT_INSTRUCTION),
        title="Your next steps",
        content="Take one tablet twice daily. Come back in two weeks.",
        timestamp=_utc(-120),
        version_number=1,
    )
    db_session.add(entry)
    db_session.add(
        Version(
            entry_id=entry.id,
            version_number=1,
            content_snapshot=entry.content,
            edited_by="u-a-clinician",
            edited_by_role=Role.CLINICIAN,
            edited_at=_utc(-120),
        )
    )
    db_session.commit()
    return patient, entry


def _mark_read(db_session, patient, *, minutes_ago: int) -> None:
    db_session.add(
        PatientView(
            user_id="u-a-patient",
            patient_id=patient.id,
            clinic_id=patient.clinic_id,
            last_viewed_at=_utc(-minutes_ago),
            view_count=1,
        )
    )
    db_session.commit()


def _correct(db_session, patient, entry, *, minutes_ago: int) -> None:
    entry.content = "Take one tablet ONCE daily. Come back in two weeks."
    entry.version_number = 2
    db_session.add(
        Version(
            entry_id=entry.id,
            version_number=2,
            content_snapshot=entry.content,
            edited_by="u-a-clinician",
            edited_by_role=Role.CLINICIAN,
            edited_at=_utc(-minutes_ago),
        )
    )
    db_session.commit()


# --- written but never read ----------------------------------------------


def test_an_instruction_the_patient_never_opened_is_unread(db_session, instruction):
    patient, _ = instruction
    states = delivery.statuses(db_session, patient)
    assert [s.state for s in states] == [delivery.UNREAD]


def test_the_clinician_is_told_how_many_are_unread(db_session, instruction):
    patient, _ = instruction
    summary = delivery.clinician_summary(db_session, patient)
    assert summary["unread_count"] == 1
    assert summary["items"][0]["label"] == "Not yet opened by the patient"


def test_reading_it_moves_the_state(db_session, instruction):
    patient, _ = instruction
    _mark_read(db_session, patient, minutes_ago=10)
    assert [s.state for s in delivery.statuses(db_session, patient)] == [delivery.READ]
    assert delivery.clinician_summary(db_session, patient)["unread_count"] == 0


# --- the scenario-12 case: corrected after she read it --------------------


def test_a_correction_after_reading_is_its_own_state(db_session, instruction):
    """She took the wrong dose on Tuesday. This is what makes that visible."""
    patient, entry = instruction
    _mark_read(db_session, patient, minutes_ago=60)
    _correct(db_session, patient, entry, minutes_ago=5)

    assert [s.state for s in delivery.statuses(db_session, patient)] == [delivery.CORRECTED]


def test_a_correction_outranks_merely_unread_for_the_clinician(db_session, instruction):
    patient, entry = instruction
    _mark_read(db_session, patient, minutes_ago=60)
    _correct(db_session, patient, entry, minutes_ago=5)

    summary = delivery.clinician_summary(db_session, patient)
    assert summary["corrected_unread_count"] == 1
    assert "acting on the old version" in summary["items"][0]["label"]


def test_the_patient_is_told_plainly_that_it_changed(db_session, instruction):
    patient, entry = instruction
    _mark_read(db_session, patient, minutes_ago=60)
    _correct(db_session, patient, entry, minutes_ago=5)

    corrections = delivery.corrections_for_patient(db_session, patient)
    assert len(corrections) == 1
    message = corrections[0]["message"]
    assert "updated after you last read it" in message
    # No clinical shorthand, and it says what to actually do.
    assert "stop and read this one" in message


def test_a_correction_before_she_ever_read_it_is_just_unread(db_session, instruction):
    """Nothing to warn about — she was never acting on the old version."""
    patient, entry = instruction
    _correct(db_session, patient, entry, minutes_ago=5)
    assert [s.state for s in delivery.statuses(db_session, patient)] == [delivery.UNREAD]


# --- the patient who has no login at all (scenario 1) ---------------------


def test_a_patient_with_no_login_is_reported_as_unreachable(db_session, instruction):
    """'She has not read it' and 'she cannot read it' are different problems."""
    patient, _ = instruction
    db_session.query(User).filter(User.id == "u-a-patient").delete()
    db_session.commit()

    summary = delivery.clinician_summary(db_session, patient)
    assert summary["reachable"] is False
    assert summary["items"][0]["label"].startswith("No patient login exists")


# --- the view must not clear the warning it is showing --------------------


def test_reading_the_page_does_not_swallow_the_correction(db_session, instruction):
    """The D-060 defect, in a new place.

    `touch_view` rolls the read marker forward on page load. If corrections were
    computed after it, the warning would vanish on the exact page load meant to
    show it.
    """
    from app.services.glance import build_patient_glance

    patient, entry = instruction
    _mark_read(db_session, patient, minutes_ago=60)
    _correct(db_session, patient, entry, minutes_ago=5)

    payload = build_patient_glance(db_session, user_id="u-a-patient", patient=patient)
    assert len(payload["corrections"]) == 1, "the correction was cleared by its own page load"
