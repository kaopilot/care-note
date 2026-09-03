"""Regressions for the four defects found in the final audit pass.

All four were live against a green suite — 847 backend tests and 67 frontend
tests passed while every one of these was reproducible. They are grouped here
rather than scattered into the files that own each subject so that a reviewer
can run one command and see the pass's findings; the modules themselves carry
the reasoning inline and cite the decision records.

  1. `delivery` imported `PATIENT_FACING_TYPES` from `core.enums` (three types,
     including `patient_note`) while `dosage_gate` imported a constant of the
     same name from `security.policy` (two types, excluding it). The patient's
     own note was therefore treated as clinic-authored content that had failed
     to reach her. (D-100)

  2. `contradictions` bound the first dose in a sentence to every drug named in
     it, so an ordinary medication list manufactured HIGH-severity dose
     disagreements between entries that agreed. (D-101)

  3. `dosage.check_text` searched a fixed window after the drug name that ran
     through the next drug, so "metformin and amlodipine 5mg" read as metformin
     5mg and blocked a patient-facing write. Same defect, opposite direction:
     one over-collected across a sentence, the other across a boundary it never
     set. (D-101)

  4. `decay.compress` rewrites `Entry.content` without creating a version, and
     staleness was defined purely as a version-number comparison — so a
     compressed entry reported `stale=False` while its highlights' offsets
     pointed into a summary. (D-102)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.enums import (
    DecayState,
    EntryType,
    HighlightStatus,
    RiskLevel,
    Role,
)
from app.models import Entry, Highlight, Patient, PatientView, Version
from app.ai import redaction
from app.services import contradictions, decay, delivery, dosage
from app.services import highlights as highlight_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _entry(eid: str, content: str, *, days: int = 0, etype=EntryType.STAFF_NOTE,
           role=Role.STAFF, risk=RiskLevel.NONE, patient="patient-a1",
           clinic="clinic-a") -> Entry:
    return Entry(
        id=eid, patient_id=patient, clinic_id=clinic, author_id="u-a-staff",
        author_role=role, type=etype, content=content, title=None,
        risk_level=risk, version_number=1,
        timestamp=_now() - timedelta(days=days),
        provenance_pointer=f"entry://{eid}",
    )


# --------------------------------------------------------------------------
# 1. The patient's own note is not undelivered clinic content (D-100)
# --------------------------------------------------------------------------


def test_a_patient_note_is_never_reported_as_undelivered(seeded):
    """She wrote it. It cannot have failed to reach her.

    The clinician's delivery panel exists to answer "did what we sent her
    land?". Counting her own note as unread inflates the number the panel is
    read for and puts a row on it that no clinician can act on.
    """
    db = seeded["db"]
    patient = db.get(Patient, "patient-a1")
    db.add(_entry("e-own", "Evening dose is the hard one.",
                  etype=EntryType.PATIENT_NOTE, role=Role.PATIENT))
    db.commit()

    summary = delivery.clinician_summary(db, patient)

    assert summary["unread_count"] == 0
    assert [item["type"] for item in summary["items"]] == []


def test_a_patient_is_not_warned_that_her_own_note_was_corrected(seeded):
    """The correction banner is the highest-severity thing the patient view says.

    It means "the clinic changed something you already acted on — possibly a
    dose". Firing it because she edited her own note is the alert-fatigue
    failure scenario 12 is about, aimed at the one reader with no way to tell a
    real warning from a spurious one.
    """
    db = seeded["db"]
    patient = db.get(Patient, "patient-a1")
    entry = _entry("e-own", "Feet tingling at night.",
                   etype=EntryType.PATIENT_NOTE, role=Role.PATIENT)
    entry.version_number = 2
    db.add(entry)
    db.flush()

    # She wrote it, opened her record, then came back and added a line. That
    # sequence — read between two versions — is exactly what CORRECTED means,
    # and without all three rows this test passes for the wrong reason.
    db.add_all([
        Version(id="v-own-1", entry_id="e-own", version_number=1,
                content_snapshot="Feet tingling at night.",
                edited_by="u-a-patient", edited_by_role=Role.PATIENT,
                edited_at=_now() - timedelta(hours=3), change_summary="created"),
        PatientView(user_id="u-a-patient", patient_id="patient-a1",
                    clinic_id="clinic-a",
                    last_viewed_at=_now() - timedelta(hours=2),
                    previous_viewed_at=_now() - timedelta(hours=2), view_count=1),
        Version(id="v-own-2", entry_id="e-own", version_number=2,
                content_snapshot="Feet tingling at night. Worse this week.",
                edited_by="u-a-patient", edited_by_role=Role.PATIENT,
                edited_at=_now() - timedelta(hours=1), change_summary="edit"),
    ])
    db.commit()

    assert delivery.corrections_for_patient(db, patient) == []

    # Guard: the same sequence on a clinician-authored instruction MUST produce
    # the banner, or the assertion above is silence rather than a result.
    instruction = _entry("e-instr", "Metformin 500mg with your evening meal.",
                         etype=EntryType.PATIENT_INSTRUCTION, role=Role.CLINICIAN)
    instruction.version_number = 2
    db.add(instruction)
    db.flush()
    db.add_all([
        Version(id="v-instr-1", entry_id="e-instr", version_number=1,
                content_snapshot="Metformin 500mg with your evening meal.",
                edited_by="u-a-clinician", edited_by_role=Role.CLINICIAN,
                edited_at=_now() - timedelta(hours=3), change_summary="created"),
        Version(id="v-instr-2", entry_id="e-instr", version_number=2,
                content_snapshot="Metformin 1g with your evening meal.",
                edited_by="u-a-clinician", edited_by_role=Role.CLINICIAN,
                edited_at=_now() - timedelta(hours=1), change_summary="dose corrected"),
    ])
    db.commit()

    assert [c["entry_id"] for c in delivery.corrections_for_patient(db, patient)] == [
        "e-instr"
    ]


def test_an_instruction_written_for_the_patient_still_reports(seeded):
    """The control. Narrowing the type set must not silence the real case."""
    db = seeded["db"]
    patient = db.get(Patient, "patient-a1")
    db.add(_entry("e-instr", "Take metformin with your evening meal.",
                  etype=EntryType.PATIENT_INSTRUCTION, role=Role.CLINICIAN))
    db.commit()

    summary = delivery.clinician_summary(db, patient)

    assert summary["unread_count"] == 1
    assert summary["items"][0]["type"] == str(EntryType.PATIENT_INSTRUCTION)


def test_the_two_patient_facing_constants_no_longer_collide():
    """One name, one meaning.

    The defect was not the contents of either set — both were right for their
    own module. It was that `from ... import PATIENT_FACING_TYPES` had two
    possible answers and neither import site looked wrong.
    """
    from app.core import enums
    from app.security import policy

    assert not hasattr(enums, "PATIENT_FACING_TYPES")
    assert policy.PATIENT_FACING_TYPES == frozenset(
        {EntryType.PATIENT_SUMMARY, EntryType.PATIENT_INSTRUCTION}
    )


# --------------------------------------------------------------------------
# 2 & 3. A dose belongs to one drug (D-101)
# --------------------------------------------------------------------------

RECONCILIATION = "Continue metformin 1g BD, amlodipine 5mg OD, atorvastatin 20mg ON."


def test_a_medication_list_binds_each_dose_to_its_own_drug():
    doses = {m.drug: m.dose for m in dosage.drug_doses(RECONCILIATION)}
    assert doses["metformin"] == ("1", "g")
    assert doses["amlodipine"] == ("5", "mg")
    assert doses["atorvastatin"] == ("20", "mg")


def test_a_medication_list_does_not_manufacture_a_dose_disagreement():
    """The shape that made this urgent: two entries that agree.

    Before the fix the first dose in the list was attached to every drug in it,
    so this pair reported "amlodipine recorded as 1g in one entry and 5mg in
    another" — at HIGH severity, on the Glance View, citing a dose that does not
    exist for that drug. A contradiction detector that is wrong about an easy
    case is worse than one with gaps, because it teaches people the flag means
    nothing.
    """
    found = contradictions.detect([
        _entry("e1", RECONCILIATION, days=2),
        _entry("e2", "Patient taking amlodipine 5mg daily as prescribed.", days=1),
    ])
    assert found == []


def test_a_real_dose_disagreement_still_fires():
    """The control for the two tests above."""
    found = contradictions.detect([
        _entry("e1", "Metformin 1g BD started today.", days=2),
        _entry("e2", "Patient continues metformin 500mg BD.", days=1),
    ])
    assert [c.kind for c in found] == ["dose_disagreement"]
    assert found[0].subject == "metformin"


def test_a_dose_is_not_read_across_an_intervening_drug_name():
    """The patient-release gate's version of the same defect.

    "Discussed metformin and amlodipine 5mg daily" gave metformin amlodipine's
    5mg, which is an order of magnitude under metformin's range, which made it
    `implausible`, which raised a 409 and put a confirmation dialog in front of
    a clinician writing an ordinary sentence.
    """
    assert dosage.check_text("Discussed metformin and amlodipine 5mg daily.") == []
    assert dosage.blocking_findings("Discussed metformin and amlodipine 5mg daily.") == []


def test_a_dose_written_before_its_drug_is_still_read():
    """Recall, not a false positive — and it is the gate that needed it.

    "Take 20mg atorvastatin at night" is how an instruction is actually
    written, and the old forward-only window saw no dose at all. That is a
    silent miss on the patient-facing path, so a decimal slip in the same
    phrasing was also unseen.
    """
    doses = {m.drug: m.dose for m in dosage.drug_doses("Take 20mg atorvastatin at night.")}
    assert doses["atorvastatin"] == ("20", "mg")
    assert [f.state for f in dosage.check_text("Take 800mg atorvastatin at night.")] == [
        dosage.IMPLAUSIBLE
    ]


def test_a_nearby_number_that_is_not_a_dose_is_not_read_as_one():
    """The cost of looking backwards is false positives, so bound it."""
    assert dosage.check_text("BP 120/80 recorded, amlodipine reviewed.") == []
    assert dosage.check_text("Weight 74.2kg. Metformin discussed.") == []


def test_the_decimal_slip_still_blocks():
    """The case the gate exists for, unchanged."""
    findings = dosage.blocking_findings("Metformin 5000mg daily.")
    assert [f.drug for f in findings] == ["metformin"]
    assert findings[0].state == dosage.IMPLAUSIBLE


# --------------------------------------------------------------------------
# 4. Compression is a content change, so provenance goes stale (D-102)
# --------------------------------------------------------------------------

_BODY = (
    "Routine review. Patient reports mild ankle swelling in the evenings. "
    "Discussed reducing salt intake and keeping a symptom diary. "
    "Home readings look acceptable overall. Follow up in three months."
)


def _entry_with_highlight(db, phrase: str = "mild ankle swelling"):
    entry = _entry("e-old", _BODY, days=400, etype=EntryType.CLINICIAN_SECTION,
                   role=Role.CLINICIAN, risk=RiskLevel.LOW)
    db.add(entry)
    db.flush()
    db.add(Version(
        id="v-old-1", entry_id="e-old", version_number=1, content_snapshot=_BODY,
        title_snapshot=None, risk_level_snapshot=str(RiskLevel.LOW),
        edited_by="u-a-clinician", edited_by_role=Role.CLINICIAN,
        edited_at=_now() - timedelta(days=400), change_summary="created",
    ))
    start = _BODY.index(phrase)
    highlight = Highlight(
        id="h-old", entry_id="e-old", patient_id="patient-a1", clinic_id="clinic-a",
        span_start=start, span_end=start + len(phrase), span_text=phrase,
        risk_reason="Symptom worth tracking", provenance_pointer="entry://e-old",
        status=HighlightStatus.SUGGESTED, created_by="system",
        created_by_role=Role.SYSTEM, score=0.5, source_version_number=1,
    )
    db.add(highlight)
    db.commit()
    return entry, highlight


def test_compression_marks_dependent_highlights_stale(seeded):
    """Archival does not create a version, so version numbers cannot detect it.

    The entry is not edited and `version_number` never moves, but every
    character of `Entry.content` is replaced. Reading staleness off the version
    number alone reported this as current.
    """
    db = seeded["db"]
    entry, highlight = _entry_with_highlight(db)
    assert highlight_service.is_stale(highlight, entry) is False

    decay.compress(db, entry)
    db.commit()

    assert str(entry.decay_state) == str(DecayState.COLD)
    assert entry.version_number == 1, "compression is not an authorship event"
    assert highlight_service.is_stale(highlight, entry) is True


def test_a_compressed_entry_does_not_offer_a_fragment_as_current_text(seeded):
    """The offsets index the original; the content is now a different string.

    Slicing anyway produced `'ing in the evenings'` — a fragment starting
    mid-word, rendered in the position where the UI shows "what the source says
    now". None is the honest answer, and the archived original is restorable.
    """
    db = seeded["db"]
    entry, highlight = _entry_with_highlight(db)
    decay.compress(db, entry)
    db.commit()

    assert highlight_service.current_text(entry, highlight) is None


def test_the_highlighted_words_survive_compression(seeded):
    """Stale must not mean lost.

    The highlight still resolves — against the version snapshot it was anchored
    to, which compression does not touch. This is what makes the pointer
    addressable rather than decorative: the words a clinician marked are still
    retrievable after the note they came from has been shortened.
    """
    db = seeded["db"]
    entry, highlight = _entry_with_highlight(db)
    decay.compress(db, entry)
    db.commit()

    assert highlight_service.anchored_text(db, highlight, entry) == "mild ankle swelling"
    assert decay.archived_original(db, entry) == _BODY


def test_stale_carries_the_reason_it_is_stale(seeded):
    """"Edited" and "archived" need different words in front of a clinician.

    Compression leaves the version number alone, so the edit wording renders as
    "v1 → v1" — a sentence that reads like a bug and explains nothing. Surfacing
    the reason is what let the UI say something true about each case.
    """
    db = seeded["db"]
    entry, highlight = _entry_with_highlight(db)
    assert highlight_service.stale_reason(highlight, entry) is None

    entry.version_number = 2
    assert highlight_service.stale_reason(highlight, entry) == "edited"

    entry.version_number = 1
    decay.compress(db, entry)
    db.commit()
    assert entry.version_number == 1
    assert highlight_service.stale_reason(highlight, entry) == "archived"


def test_restoring_an_entry_makes_its_highlights_current_again(seeded):
    """Reversibility is the reason cold is safe. It has to reach provenance too."""
    db = seeded["db"]
    entry, highlight = _entry_with_highlight(db)
    decay.compress(db, entry)
    db.commit()
    assert highlight_service.is_stale(highlight, entry) is True

    decay.restore(db, entry)
    db.commit()

    assert entry.content == _BODY
    assert highlight_service.is_stale(highlight, entry) is False
    assert highlight_service.current_text(entry, highlight) == "mild ankle swelling"


# --------------------------------------------------------------------------
# 5. A Malaysian mobile is a phone number (D-105)
# --------------------------------------------------------------------------
#
# Found by running a consult transcript through the capture pipeline and reading
# what the transcript panel would put on screen. The name and the IC came back
# redacted; the phone number did not. There was a Singapore local pattern and no
# Malaysian one, so a MY mobile was only caught when it happened to follow a cue
# word — and "confirm your number is 019-888 7777" has no cue.


MY_FORMATS = [
    "019-888 7777",   # how it is normally written
    "019 888 7777",
    "0198887777",
    "012-345 6789",
    "03-7960 1234",   # landline
]


def test_a_malaysian_number_is_redacted_however_it_is_written():
    for number in MY_FORMATS:
        assert redaction.redact_phi(number) == "[PHONE_1]", number


def test_a_malaysian_number_is_redacted_without_a_cue_word():
    """The sentence that exposed this, from a real consult transcript.

    The cue-anchored pattern wants "call", "hp", "tel". A clinician confirming a
    number says "your number is", which is not a cue, so the number travelled to
    the model and into the stored transcript.
    """
    out = redaction.redact_phi(
        "Confirm your number is 019-888 7777 and IC 680311-14-5566."
    )
    assert "019" not in out and "7777" not in out
    assert out == "Confirm your number is [PHONE_1] and IC [ID_1]."


def test_the_residual_tripwire_also_sees_it():
    """The fail-closed check has to know about the pattern too.

    `find_residual_phi` shares its regexes with the redactor, which D-095 flagged
    as a hazard and this build accepted. The consequence is concrete: before this
    fix the redactor missed a MY mobile *and* the tripwire reported the output
    clean, so nothing anywhere in the system noticed.
    """
    assert redaction.find_residual_phi("call me on 019-888 7777") != []
    assert redaction.find_residual_phi(redaction.redact_phi("hp 019-888 7777")) == []


def test_clinical_numbers_are_not_mistaken_for_phone_numbers():
    """The cost of a looser pattern is false positives, so bound it.

    Redaction is accuracy as much as privacy — the hint says so directly. A
    pattern that ate `BP 120/80` or a dose would corrupt the clinical record to
    protect a number that was never there.
    """
    untouched = [
        "BP 120/80, HbA1c 8.4%",
        "metformin 1g BD, amlodipine 5mg OD",
        "Seen 03 Feb 2026",
        "Weight 74.2kg, eGFR 88",
        "Dose 0.5 mg nightly",
        "Temperature 37.2, pulse 88",
    ]
    for text in untouched:
        assert redaction.redact_phi(text) == text, text


def test_an_identity_number_starting_with_zero_is_still_an_id():
    """Anyone born from 2000 has an NRIC that opens with a zero.

    The MY phone pattern would match it, so ordering carries the correctness:
    the NRIC and MyKad passes run first and the phone pass never sees it. If
    that order were swapped, an identity number would be labelled `[PHONE_n]` —
    still redacted, but the wrong category in the audit trail.
    """
    out = redaction.redact_phi("IC 010311-14-5566 registered today.")
    assert "[ID_1]" in out
    assert "PHONE" not in out
