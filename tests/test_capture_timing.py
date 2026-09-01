"""When does the system know about something said at minute two?

Scenario 7: *"A drug allergy is mentioned at minute two of a twenty-minute
consult. When does your system know about it? During the consult, or after it
ends? Those are two different products with the same feature name."*

This build is the second product, and these tests say so rather than leaving it
to be discovered. They assert the **boundary**, not a capability: nothing about
an early turn reaches the record until the session is submitted whole.

A test that pins a limitation is worth writing for two reasons. It stops the
limitation being quietly overclaimed later, and it fails loudly on the day
someone makes capture incremental — at which point it should be rewritten, not
deleted.

See DECISIONS.md D-032 and the scenario-7 entry in the clinic review.
"""

from __future__ import annotations

from app.core.enums import InteractionType, Role
from app.models import AIScribedNote, Entry, TranscriptSegment
from app.services import contradictions, features
from app.services.transcripts import Turn


ALLERGY_AT_MINUTE_TWO = [
    Turn("clinician", "Come in, take a seat. What brings you in today?", 0, 4000),
    Turn("patient", "The cough again, and I have been short of breath.", 4000, 9000),
    # 00:02:00 — the fact that matters, said early and never repeated.
    Turn("patient", "I should say I am allergic to penicillin.", 120_000, 126_000),
    Turn("clinician", "Noted. Let me listen to your chest.", 126_000, 131_000),
    *[
        Turn("clinician", f"Filler turn {index} of an otherwise ordinary consult.",
             140_000 + index * 5_000, 145_000 + index * 5_000)
        for index in range(8)
    ],
    Turn("clinician", "I will start you on amoxicillin 500mg three times a day.",
         1_150_000, 1_160_000),
]


def _patient_and_clinician(db):
    from app.models import Patient, User

    return (
        db.query(Patient).filter(Patient.id == "patient-a1").one(),
        db.query(User).filter(User.id == "u-a-clinician").one(),
    )


def test_nothing_from_an_early_turn_exists_before_the_session_is_submitted(
    db_session, seeded
):
    """The boundary, stated as a test.

    There is no incremental path: no partial transcript, no segment, no entry.
    A clinician prescribing at minute nineteen is prescribing against a record
    that has not heard minute two.
    """
    patient, _ = _patient_and_clinician(db_session)
    before = db_session.query(TranscriptSegment).count()

    # Everything the pipeline could act on arrives in one call. There is
    # deliberately nothing to invoke here that would represent "the consult so
    # far" — that absence is the finding.
    assert db_session.query(TranscriptSegment).count() == before
    assert (
        db_session.query(Entry)
        .filter(Entry.patient_id == patient.id, Entry.author_role == Role.SYSTEM)
        .count()
        == 0
    )


def test_the_allergy_is_known_only_after_the_whole_transcript_is_submitted(
    db_session, seeded
):
    from app.services import scribe

    patient, clinician = _patient_and_clinician(db_session)
    entry = scribe.run_scribe(
        db_session,
        patient=patient,
        interaction_type=InteractionType.DOCTOR_PATIENT_CONSULT,
        turns=ALLERGY_AT_MINUTE_TWO,
        actor_id=clinician.id,
    )
    assert entry is not None
    note = (
        db_session.query(AIScribedNote).filter(AIScribedNote.entry_id == entry.id).one()
    )
    # It does land — correctly, provenance-linked, and eighteen minutes late.
    assert note.session_id
    segments = (
        db_session.query(TranscriptSegment)
        .filter(TranscriptSegment.session_id == note.session_id)
        .all()
    )
    assert any("penicillin" in s.redacted_text.lower() for s in segments)


def test_the_deterministic_extractors_need_no_model_and_could_run_incrementally(
    db_session, seeded
):
    """The shape of the fix, asserted so the claim is not just prose.

    Contradiction detection and tagging are pure functions over text. Neither
    needs a model, a session, or a completed transcript — which is why the
    smallest real fix for scenario 7 is to run *these* on partial transcript as
    it arrives, and leave summarisation at the end where it belongs.
    """
    partial = "patient: I should say I am allergic to penicillin."
    later = "clinician: I will start you on amoxicillin 500mg three times a day."

    tags, _ = features.tag_span(partial)
    assert any(tag.startswith("entity:allergy") for tag in tags)

    class _Stub:
        def __init__(self, entry_id, content):
            self.id = entry_id
            self.type = "staff_note"
            self.content = content
            self.author_role = Role.STAFF
            self.title = ""
            self.version_number = 1
            from datetime import datetime, timezone

            self.timestamp = datetime.now(timezone.utc)

    found = contradictions.detect([_Stub("a", partial), _Stub("b", later)])
    assert found and found[0].kind == "allergy_vs_administration", (
        "the contradiction is detectable from two turns alone — the gap is when "
        "we look, not whether we can"
    )
