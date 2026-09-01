"""An allergy asserted in one entry and denied in another.

Scenario 13. The nurse recorded a penicillin allergy; the patient told the AI she
has no known allergies. Both were in the timeline and `detect()` returned **zero**
contradictions.

The cause was a guard that is correct on its own: negated mentions are dropped so
that "patient denies allergy to aspirin" never becomes a critical allergy alert.
Dropping them also discarded the patient's denial, so it was never compared
against anything. A denial is not nothing — it is a position, and it can
disagree with a position recorded elsewhere.

See DECISIONS.md D-073.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.enums import RiskLevel
from app.services import contradictions


class _Entry:
    """Minimal stand-in — detect() reads content, type, id and author_role only."""

    def __init__(self, entry_id: str, entry_type: str, content: str, role: str) -> None:
        self.id = entry_id
        self.type = entry_type
        self.content = content
        self.author_role = role
        self.timestamp = datetime.now(timezone.utc)
        self.title = ""
        self.version_number = 1


def _nurse(content: str) -> _Entry:
    return _Entry("e-nurse", "staff_note", content, "staff")


def _ai_session(content: str) -> _Entry:
    return _Entry("e-ai", "ai_patient_session_summary", content, "system")


def _clinician(content: str) -> _Entry:
    return _Entry("e-doc", "clinician_section", content, "clinician")


# --- the case from the review --------------------------------------------


def test_recorded_allergy_versus_blanket_denial_is_detected():
    found = contradictions.detect(
        [
            _nurse("Patient reports allergic to penicillin. Documented at triage."),
            _ai_session("Patient states she has no known allergies."),
        ]
    )
    assert len(found) == 1
    assert found[0].kind == "assertion_vs_denial"
    assert found[0].subject == "penicillin"


def test_the_assertion_is_reported_first():
    """The safe reading must lead: 'allergy recorded ... but denied'."""
    found = contradictions.detect(
        [
            _ai_session("Patient states she has no known allergies."),
            _nurse("Patient reports allergic to penicillin."),
        ]
    )
    assert found[0].left_entry_id == "e-nurse"
    assert "recorded" in found[0].detail


def test_severity_is_high_not_critical():
    """Nothing dangerous has happened yet — the safe action is already in force.

    Rating this critical would dilute the level that means "someone is about to
    be given a drug they react to", which is the level that must keep working.
    """
    found = contradictions.detect(
        [
            _nurse("Allergic to penicillin."),
            _ai_session("NKDA per patient."),
        ]
    )
    assert found[0].severity == RiskLevel.HIGH


def test_specific_denial_of_a_named_drug_is_detected():
    found = contradictions.detect(
        [
            _nurse("Allergic to aspirin, documented 2024."),
            _clinician("Patient denies allergy to aspirin today."),
        ]
    )
    assert len(found) == 1
    assert found[0].kind == "assertion_vs_denial"


def test_a_human_human_denial_conflict_is_marked_as_such():
    """No precedence rule exists between two humans, so it must be visible."""
    found = contradictions.detect(
        [
            _nurse("Allergic to penicillin."),
            _clinician("Patient denies any allergies."),
        ]
    )
    assert found[0].human_human is True


# --- what must NOT fire ---------------------------------------------------


def test_two_denials_do_not_contradict_each_other():
    assert (
        contradictions.detect(
            [
                _nurse("No known allergies."),
                _ai_session("Patient denies any allergies."),
            ]
        )
        == []
    )


def test_a_denial_against_an_ordinary_prescription_is_not_a_contradiction():
    """That is a normal prescription for a drug nobody claimed an allergy to."""
    assert (
        contradictions.detect(
            [
                _nurse("No known allergies."),
                _clinician("Started on amoxicillin 500mg TDS."),
            ]
        )
        == []
    )


def test_a_denial_of_one_drug_does_not_contradict_an_allergy_to_another():
    assert (
        contradictions.detect(
            [
                _nurse("Allergic to penicillin."),
                _clinician("Patient denies allergy to aspirin."),
            ]
        )
        == []
    )


def test_repeated_blanket_denials_produce_one_finding_not_several():
    found = contradictions.detect(
        [
            _nurse("Allergic to penicillin."),
            _ai_session("No known allergies. Patient again states no known allergies."),
        ]
    )
    assert len(found) == 1


# --- the existing classes must be untouched -------------------------------


def test_allergy_versus_administration_still_fires_at_critical():
    found = contradictions.detect(
        [
            _nurse("Patient reports allergic to penicillin."),
            _clinician("Started on amoxicillin 500mg TDS."),
        ]
    )
    assert found[0].kind == "allergy_vs_administration"
    assert found[0].severity == RiskLevel.CRITICAL


def test_a_denied_allergy_still_never_becomes_an_administration_alert():
    """The original guard must survive: this was the reason denials were dropped."""
    found = contradictions.detect(
        [
            _clinician("Patient denies allergy to aspirin."),
            _clinician("Continue aspirin 100mg daily."),
        ]
    )
    assert all(f.kind != "allergy_vs_administration" for f in found)
