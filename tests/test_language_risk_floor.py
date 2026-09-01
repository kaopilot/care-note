"""The risk floor must not depend on which language the patient used.

Scenario 6 and scenario 14. Two defects, one cause: the deterministic floor
matched English phrases against raw text *before* `tag_span` ran, so the tagger
was bilingual and the safety mechanism sitting on top of it was not.

Measured before the fix:

    "chest pain when I walk uphill"   -> high
    "sakit dada bila naik tangga"     -> medium      (same symptom, lower floor)
    "denies chest pain"               -> high        (a clean history, red-flagged)

See DECISIONS.md D-072.
"""

from __future__ import annotations

import pytest

from app.core.enums import InteractionType, RiskLevel
from app.models import AIScribedNote, Patient, User
from app.services import scribe
from app.services.features import HIGH_RISK_TAGS, high_risk_tags, is_unreadable


# --- the floor is language-independent ------------------------------------


@pytest.mark.parametrize(
    "english,malay",
    [
        ("I get chest pain when I walk uphill.", "Saya rasa sakit dada bila naik tangga."),
        ("She fainted yesterday at home.", "Dia pengsan semalam di rumah."),
        ("There was bleeding this morning.", "Ada berdarah pagi tadi."),
    ],
)
def test_same_symptom_same_floor_in_either_language(english, malay):
    """A patient must not get a lower risk floor for describing it in Malay."""
    assert scribe._infer_risk(english) == scribe._infer_risk(malay) == str(RiskLevel.HIGH)


def test_floor_works_in_tag_space_not_english_strings():
    """Pins the mechanism, not just the outcome.

    Checking tags is what makes the floor inherit new languages automatically.
    A future contributor who 'simplifies' this back to substring matching would
    reintroduce the defect silently, and every outcome test above would still
    pass for English.
    """
    assert high_risk_tags("Saya rasa sakit dada.") == ["symptom:chest_pain"]
    assert all(tag.startswith(("symptom:", "entity:")) for tag in HIGH_RISK_TAGS)


def test_fainting_reaches_the_floor_in_english_too():
    """`fainted` was absent from the old English list — never only a language gap."""
    assert scribe._infer_risk("She fainted at the bus stop.") == str(RiskLevel.HIGH)


# --- negation ------------------------------------------------------------


def test_a_denied_symptom_does_not_set_the_high_floor():
    """A clean history is not a red flag. Alert fatigue is how loud fails go silent."""
    assert high_risk_tags("Patient denies chest pain and no shortness of breath.") == []
    assert scribe._infer_risk("Patient denies chest pain.") != str(RiskLevel.HIGH)


def test_a_denied_symptom_is_still_recorded_as_medium():
    """Downgraded, never dropped. A pertinent negative is still clinical content."""
    assert scribe._infer_risk("Patient denies chest pain.") == str(RiskLevel.MEDIUM)


def test_one_assertion_outweighs_any_number_of_denials():
    """Asymmetric on purpose: 'no chest pain Monday, chest pain today' is high."""
    text = "No chest pain on Monday. No chest pain Tuesday. Chest pain today."
    assert high_risk_tags(text) == ["symptom:chest_pain"]
    assert scribe._infer_risk(text) == str(RiskLevel.HIGH)


# --- abstention on content the system cannot read -------------------------


def test_unreadable_content_is_detected():
    assert is_unreadable(
        "Ka joah tioh e kha there thiam thiam, bo hoat tou khun lah.", "nan"
    )


def test_ordinary_english_with_no_clinical_content_is_not_flagged():
    """Conservative. Small talk produces no tags and is not a gap in understanding."""
    assert not is_unreadable("Okay thank you doctor, see you next week then.", "en")


def test_short_turns_are_not_flagged():
    assert not is_unreadable("bo hoat tou", "nan")


def test_supported_language_is_never_flagged_even_without_tags():
    assert not is_unreadable("Sila duduk di sini dan tunggu sebentar ya.", "ms")


def test_scribe_records_unread_segments(db_session, seeded):
    """The clinical capture fixture contains one romanised Hokkien turn."""
    patient = db_session.query(Patient).filter(Patient.id == "patient-a1").one()
    clinician = db_session.query(User).filter(User.id == "u-a-clinician").one()

    entry = scribe.run_scribe(
        db_session,
        patient=patient,
        interaction_type=InteractionType.DOCTOR_PATIENT_CONSULT,
        actor_id=clinician.id,
    )
    note = (
        db_session.query(AIScribedNote).filter(AIScribedNote.entry_id == entry.id).one()
    )
    assert note.unreadable_segment_count >= 1, (
        "the Hokkien turn should be counted as unread, not silently ignored"
    )
