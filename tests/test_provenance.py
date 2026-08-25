"""Provenance pointer round-tripping and resolution.

Phase 3's `test_highlight_provenance.py` builds on this; here we prove the
pointer scheme itself is sound before anything stores one.
"""

from __future__ import annotations

import pytest

from app.core.enums import EntryType, RiskLevel, Role
from app.core.provenance import (
    ProvenanceError,
    entry_pointer,
    parse,
    resolve,
    session_pointer,
    transcript_pointer,
)
from app.models import AIScribedNote, Entry, TranscriptSegment


def test_round_trip_entry_pointer() -> None:
    pointer = entry_pointer("e-123", 10, 42)
    parsed = parse(pointer)
    assert parsed.scheme == "entry"
    assert parsed.target_id == "e-123"
    assert (parsed.span_start, parsed.span_end) == (10, 42)
    assert str(parsed) == pointer


def test_round_trip_session_and_transcript_pointers() -> None:
    assert parse(session_pointer("sess-9", turn=3)).index == 3
    assert parse(transcript_pointer("sess-9", 7)).index == 7


@pytest.mark.parametrize(
    "bad",
    ["", "not-a-pointer", "entry://", "ftp://x", "entry://e-1#span:50-10", "entry://e-1#span:5"],
)
def test_malformed_pointers_raise(bad: str) -> None:
    with pytest.raises(ProvenanceError):
        parse(bad)


def _make_entry(db, entry_id="e-1", clinic="clinic-a", content="Start warfarin 3mg nightly."):
    entry = Entry(
        id=entry_id,
        patient_id="patient-a1",
        clinic_id=clinic,
        author_role=Role.CLINICIAN,
        author_id="u-a-clinician",
        type=EntryType.CLINICIAN_SECTION,
        content=content,
        risk_level=RiskLevel.HIGH,
    )
    db.add(entry)
    db.commit()
    return entry


def test_resolve_entry_span_returns_exact_text(seeded) -> None:
    db = seeded["db"]
    _make_entry(db)
    pointer = entry_pointer("e-1", 6, 14)
    resolved = resolve(db, pointer, clinic_id="clinic-a")
    assert resolved["kind"] == "entry"
    assert resolved["span_text"] == "warfarin"


def test_resolve_rejects_cross_clinic_pointer(seeded) -> None:
    """A syntactically valid pointer must not become a read primitive across a
    clinic boundary."""
    db = seeded["db"]
    _make_entry(db, entry_id="e-b", clinic="clinic-b")
    with pytest.raises(ProvenanceError, match="clinic boundary"):
        resolve(db, entry_pointer("e-b"), clinic_id="clinic-a")


def test_dangling_pointer_raises(seeded) -> None:
    with pytest.raises(ProvenanceError, match="missing entry"):
        resolve(seeded["db"], entry_pointer("does-not-exist"), clinic_id="clinic-a")


def test_span_beyond_content_raises(seeded) -> None:
    db = seeded["db"]
    _make_entry(db)
    with pytest.raises(ProvenanceError, match="exceeds entry length"):
        resolve(db, entry_pointer("e-1", 0, 9999), clinic_id="clinic-a")


def test_resolve_session_pointer_to_ai_note(seeded) -> None:
    db = seeded["db"]
    entry = _make_entry(db, entry_id="e-ai", content="AI summary body.")
    db.add(
        AIScribedNote(
            id="ai-1",
            entry_id=entry.id,
            clinic_id="clinic-a",
            session_id="sess-42",
            interaction_type="doctor_patient_consult",
            model_used="stub",
        )
    )
    db.commit()
    resolved = resolve(db, session_pointer("sess-42", turn=2), clinic_id="clinic-a")
    assert resolved["kind"] == "session"
    assert resolved["entry_id"] == "e-ai"
    assert resolved["turn"] == 2


def test_resolve_transcript_segment(seeded) -> None:
    db = seeded["db"]
    db.add(
        TranscriptSegment(
            id="seg-1",
            session_id="sess-42",
            clinic_id="clinic-a",
            sequence=3,
            speaker_label="clinician",
            start_ms=12000,
            end_ms=15500,
            redacted_text="Any chest pain since we last spoke?",
            confidence=0.91,
        )
    )
    db.commit()
    resolved = resolve(db, transcript_pointer("sess-42", 3), clinic_id="clinic-a")
    assert resolved["speaker_label"] == "clinician"
    assert resolved["start_ms"] == 12000
