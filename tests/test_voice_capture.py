"""Phase 5 — ambient consult capture.

Not one of the four test files the brief names; those are Phase 3 and are
unchanged. This covers the bonus surface, and it concentrates on the three
claims a reviewer cannot check by reading the UI:

1. the capture view boundary is enforced server-side, not by which button the
   client draws;
2. identifiers are removed before any text reaches a model, and the recording
   itself is never kept;
3. every summary line that claims a spoken source resolves to one.
"""

from __future__ import annotations

import json

import pytest

from app.ai import asr_client
from app.core.enums import CaptureKind, EntryType, Role
from app.core.provenance import ProvenanceError, resolve
from app.models import CaptureSession, SummaryAttribution, TranscriptSegment
from app.services import capture

# Not audio. The stub recogniser is deterministic on the digest and never
# decodes the container, so any bytes exercise the same path a real file would.
FAKE_AUDIO = b"\x1aE\xdf\xa3" + b"synthetic-consult-audio" * 300


@pytest.fixture()
def clinician(token_for):
    return token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")


@pytest.fixture()
def staff(token_for):
    return token_for("u-a-staff", Role.STAFF, "clinic-a")


@pytest.fixture()
def patient(token_for):
    return token_for("u-a-patient", Role.PATIENT, "clinic-a", patient_id="patient-a1")


@pytest.fixture()
def admin(token_for):
    return token_for("u-a-admin", Role.ADMIN, "clinic-a")


@pytest.fixture()
def other_clinic(token_for):
    return token_for("u-b-clinician", Role.CLINICIAN, "clinic-b")


def post_audio(client, headers, patient_id="patient-a1", kind="clinical", **form):
    payload = {"kind": kind, "source": "live_recording", "duration_ms": "62000"}
    payload.update(form)
    return client.post(
        f"/patients/{patient_id}/capture",
        data=payload,
        files={"audio": ("consult.webm", FAKE_AUDIO, "audio/webm")},
        headers=headers,
    )


# --------------------------------------------------------------------------
# The minimum viable pipeline
# --------------------------------------------------------------------------


def test_audio_upload_produces_an_ai_scribed_entry(client_p1, clinician):
    """Audio in, correctly-typed timeline entry out."""
    response = post_audio(client_p1, clinician)
    assert response.status_code == 201, response.text
    body = response.json()

    entry = body["entry"]
    assert entry["type"] == str(EntryType.AI_DOCTOR_CONSULT_SUMMARY)
    assert entry["author_role"] == str(Role.SYSTEM)
    assert entry["author_id"] == "system"
    assert entry["is_ai_scribed"] is True
    # Points at the session, not at itself: the note is derived text.
    assert entry["provenance_pointer"].startswith("session://")
    assert entry["ai_session_id"] == body["capture"]["session_id"]
    assert entry["content"].strip()


def test_transcript_upload_needs_no_recogniser(client_p1, staff):
    """The path that proves the pipeline without a working microphone."""
    transcript = (
        "staff: Good afternoon, I'm the nurse today.\n"
        "patient: My ankle has been swollen for four days.\n"
        "staff: I'll arrange a blood pressure check and flag this to the doctor.\n"
    )
    response = client_p1.post(
        "/patients/patient-a1/capture",
        data={"kind": "clinical", "transcript": transcript},
        headers=staff,
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["capture"]["source"] == "transcript_upload"
    # Nothing was recognised, so no recogniser is credited.
    assert body["capture"]["asr_provider"] == "none"
    assert body["capture"]["transcription_simulated"] is False
    assert body["capture"]["audio_bytes_received"] == 0
    # Staff role -> nurse consult, derived from the token.
    assert body["entry"]["type"] == str(EntryType.AI_NURSE_CONSULT_SUMMARY)


def test_entry_type_follows_the_role_not_the_request(client_p1, staff, clinician):
    """A caller cannot enter a recording as a kind of encounter they were not in."""
    staff_entry = post_audio(client_p1, staff).json()["entry"]
    clinician_entry = post_audio(client_p1, clinician).json()["entry"]

    assert staff_entry["type"] == str(EntryType.AI_NURSE_CONSULT_SUMMARY)
    assert clinician_entry["type"] == str(EntryType.AI_DOCTOR_CONSULT_SUMMARY)


def test_capture_appears_in_the_timeline(client_p1, clinician):
    response = post_audio(client_p1, clinician)
    entry_id = response.json()["entry"]["id"]

    timeline = client_p1.get("/patients/patient-a1/entries", headers=clinician).json()
    assert entry_id in {row["id"] for row in timeline}


def test_empty_submission_is_refused(client_p1, clinician):
    response = client_p1.post(
        "/patients/patient-a1/capture", data={"kind": "clinical"}, headers=clinician
    )
    assert response.status_code == 400
    assert "transcript" in response.json()["detail"].lower()


def test_unreadable_transcript_is_refused(client_p1, clinician):
    response = client_p1.post(
        "/patients/patient-a1/capture",
        data={"kind": "clinical", "transcript": "....\n....\n"},
        headers=clinician,
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# The view boundary, enforced server-side
# --------------------------------------------------------------------------


def test_patient_cannot_submit_a_clinical_capture(client_p1, patient):
    """Clinical voice capture is clinical-view only — enforced on the server."""
    response = post_audio(client_p1, patient, kind="clinical")
    assert response.status_code == 403
    assert "clinical view" in response.json()["detail"]


def test_clinician_cannot_submit_a_patient_capture(client_p1, clinician):
    """And the converse. Neither direction is a UI-only distinction."""
    response = post_audio(client_p1, clinician, kind="patient")
    assert response.status_code == 403
    assert "patient view" in response.json()["detail"]


def test_patient_capture_is_accepted_and_returns_a_receipt_only(client_p1, patient):
    """A patient may record, and gets a receipt rather than the clinical note.

    The summary written from their recording goes to the care team. Handing it
    back here would route around the patient-facing filter the timeline
    enforces (D-049).
    """
    response = post_audio(client_p1, patient, kind="patient")
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["entry"] is None
    assert body["attribution_coverage"] is None
    assert body["capture"]["kind"] == "patient"
    assert "not kept" in body["message"]


def test_admin_cannot_capture(client_p1, admin):
    """Oversight, not authorship — refused by the dependency, before any handler."""
    assert post_audio(client_p1, admin).status_code == 403


def test_patient_cannot_read_a_transcript_even_of_their_own_capture(
    client_p1, patient, clinician
):
    """The raw transcript is more raw than a raw AI-scribed note, and a consult
    recorded in the patient view contains the clinician's half too (D-049)."""
    session_id = post_audio(client_p1, patient, kind="patient").json()["capture"][
        "session_id"
    ]
    # The clinical role can read it...
    assert client_p1.get(f"/captures/{session_id}", headers=clinician).status_code == 200
    # ...the patient who made it cannot.
    assert client_p1.get(f"/captures/{session_id}", headers=patient).status_code == 403


def test_capture_cannot_be_written_across_a_clinic_boundary(client_p1, other_clinic):
    response = post_audio(client_p1, other_clinic)
    assert response.status_code == 404


def test_capture_cannot_be_read_across_a_clinic_boundary(
    client_p1, clinician, other_clinic
):
    session_id = post_audio(client_p1, clinician).json()["capture"]["session_id"]
    assert (
        client_p1.get(f"/captures/{session_id}", headers=other_clinic).status_code == 404
    )


def test_unauthenticated_capture_is_refused(client_p1):
    response = client_p1.post(
        "/patients/patient-a1/capture", data={"kind": "clinical", "transcript": "x: y"}
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------
# Redaction, and the recording that was never kept
# --------------------------------------------------------------------------


def test_identifiers_are_stripped_before_storage_and_before_the_model(
    client_p1, clinician, seeded_p1
):
    """The stub recogniser plants a name, an NRIC and a phone number precisely
    so this assertion has something to prove."""
    body = post_audio(client_p1, clinician).json()
    session_id = body["capture"]["session_id"]

    assert body["capture"]["redaction_count"] >= 3

    segments = (
        seeded_p1["db"]
        .query(TranscriptSegment)
        .filter(TranscriptSegment.session_id == session_id)
        .all()
    )
    assert segments
    stored = "\n".join(segment.redacted_text for segment in segments)

    # Nothing identifying survives at rest...
    assert "S8412345D" not in stored
    assert "6123 4567" not in stored
    assert "Amira" not in stored
    assert "Rahman" not in stored
    # ...and the placeholders are there instead.
    assert "[ID_" in stored and "[PHONE_" in stored and "[NAME_" in stored

    # The generated summary inherits the redacted text, never the original.
    assert "Amira" not in body["entry"]["content"]
    assert "S8412345D" not in body["entry"]["content"]


def test_audio_is_never_retained(client_p1, clinician, seeded_p1):
    """The strongest identifier in a consult is the voice. It is not stored.

    Asserted against the database rather than the response so this is a fact
    about the record, not about one serialiser.
    """
    session_id = post_audio(client_p1, clinician).json()["capture"]["session_id"]
    row = (
        seeded_p1["db"]
        .query(CaptureSession)
        .filter(CaptureSession.session_id == session_id)
        .one()
    )

    assert row.audio_retained is False
    assert row.audio_bytes_received == len(FAKE_AUDIO)

    # No column anywhere on the capture holds the bytes we were handed.
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, (bytes, bytearray)):
            pytest.fail(f"CaptureSession.{column.name} holds binary data")
        if isinstance(value, str):
            assert FAKE_AUDIO[:32].decode("latin-1") not in value


def test_simulated_transcription_says_so(client_p1, clinician):
    """A stub that cannot hear must not be mistaken for one that can."""
    body = post_audio(client_p1, clinician).json()
    assert body["capture"]["transcription_simulated"] is True
    assert body["capture"]["asr_model"] == "simulated-asr-v1"

    detail = client_p1.get(
        f"/captures/{body['capture']['session_id']}", headers=clinician
    ).json()
    assert "simulated recogniser" in detail["notice"]
    assert "not retained" in detail["notice"]


def test_remote_recogniser_fails_closed_without_explicit_opt_in(monkeypatch):
    """Audio cannot be redacted before transcription, so shipping it off-box is
    a deliberate act. The gate raises; it does not quietly use the stub."""
    from dataclasses import replace

    # Settings is a frozen dataclass, so the whole object is swapped rather
    # than a field assigned — which is also closer to how a real deployment
    # would differ: by configuration, not by mutation.
    monkeypatch.setattr(
        asr_client,
        "settings",
        replace(
            asr_client.settings, asr_provider="remote", asr_allow_audio_egress=False
        ),
    )

    with pytest.raises(asr_client.AudioEgressBlocked):
        asr_client.transcribe(
            FAKE_AUDIO, mime="audio/webm", kind="clinical", patient_name="Test Person"
        )


def test_remote_recogniser_is_reachable_once_egress_is_allowed(monkeypatch):
    """The gate is a gate, not a wall: with the opt-in set the call proceeds to
    the vendor (which this prototype deliberately does not implement)."""
    from dataclasses import replace

    monkeypatch.setattr(
        asr_client,
        "settings",
        replace(
            asr_client.settings, asr_provider="remote", asr_allow_audio_egress=True
        ),
    )

    with pytest.raises(NotImplementedError):
        asr_client.transcribe(
            FAKE_AUDIO, mime="audio/webm", kind="clinical", patient_name="Test Person"
        )


def test_oversized_and_unsupported_audio_are_refused():
    with pytest.raises(asr_client.UnsupportedAudio):
        asr_client.transcribe(b"", mime="audio/webm", kind="clinical", patient_name="X")
    with pytest.raises(asr_client.UnsupportedAudio):
        asr_client.transcribe(
            FAKE_AUDIO, mime="application/zip", kind="clinical", patient_name="X"
        )
    with pytest.raises(asr_client.UnsupportedAudio):
        asr_client.transcribe(
            b"0" * (asr_client.MAX_AUDIO_BYTES + 1),
            mime="audio/webm",
            kind="clinical",
            patient_name="X",
        )


# --------------------------------------------------------------------------
# Provenance back to transcript segments — the phase's exit criterion
# --------------------------------------------------------------------------


def test_every_attribution_resolves_to_a_real_segment(
    client_p1, clinician, seeded_p1
):
    """The Phase 5 equivalent of test_highlight_provenance: a pointer that does
    not resolve is a citation that lies."""
    body = post_audio(client_p1, clinician).json()
    entry_id = body["entry"]["id"]

    rows = client_p1.get(f"/entries/{entry_id}/attribution", headers=clinician).json()
    assert rows, "a captured consult should attribute at least one line"

    for row in rows:
        assert row["resolves"] is True
        assert row["provenance_pointer"].startswith("transcript://")
        assert "#segment:" in row["provenance_pointer"]

        # Resolve independently of the route that just claimed it resolves.
        resolved = resolve(
            seeded_p1["db"], row["provenance_pointer"], clinic_id="clinic-a"
        )
        assert resolved["kind"] == "transcript_segment"
        assert resolved["sequence"] == row["segment_sequence"]
        assert resolved["speaker_label"] == row["speaker_label"]
        assert resolved["text"].strip()


def test_attribution_spans_match_the_entry_text(client_p1, clinician):
    """Offsets must address the words they claim to, not merely be in range."""
    body = post_audio(client_p1, clinician).json()
    entry_id, content = body["entry"]["id"], body["entry"]["content"]

    for row in client_p1.get(
        f"/entries/{entry_id}/attribution", headers=clinician
    ).json():
        assert 0 <= row["span_start"] < row["span_end"] <= len(content)
        assert content[row["span_start"] : row["span_end"]] == row["span_text"]


def test_verbatim_attribution_is_provable_not_asserted(client_p1, clinician):
    """A `verbatim` link means the segment's words really are in the line."""
    body = post_audio(client_p1, clinician).json()
    rows = client_p1.get(
        f"/entries/{body['entry']['id']}/attribution", headers=clinician
    ).json()

    verbatim = [row for row in rows if row["match_type"] == "verbatim"]
    assert verbatim, "the offline summariser selects real utterances"
    for row in verbatim:
        line = " ".join(row["span_text"].lower().split())
        segment = " ".join(row["segment_text"].lower().split())
        assert segment in line


def test_unmatched_lines_get_no_attribution_row(seeded_p1):
    """No source is better than a plausible wrong one (D-048).

    Driven at the service layer so the summary text can be controlled exactly.
    """
    from app.services import attribution
    from app.models import Entry

    db = seeded_p1["db"]
    db.add_all(
        [
            TranscriptSegment(
                session_id="cap-test-1", clinic_id="clinic-a", sequence=0,
                speaker_label="clinician", start_ms=0, end_ms=2000,
                redacted_text="The ankle swelling is a side effect of amlodipine.",
                confidence=0.9, language="en",
            ),
        ]
    )
    entry = Entry(
        id="entry-attr-test", patient_id="patient-a1", clinic_id="clinic-a",
        author_role=Role.SYSTEM, author_id="system",
        type=EntryType.AI_DOCTOR_CONSULT_SUMMARY,
        content=(
            "Consult summary.\n\nKey points\n"
            "- The ankle swelling is a side effect of amlodipine.\n"
            "- Patient plans to travel to Penang next month for a wedding.\n"
        ),
        version_number=1,
    )
    db.add(entry)
    db.flush()

    rows = attribution.link_summary_to_segments(
        db, entry=entry, session_id="cap-test-1"
    )
    assert len(rows) == 1
    assert rows[0].match_type == "verbatim"
    # The unrelated line is simply absent rather than pointed somewhere.
    assert "Penang" not in rows[0].provenance_pointer

    report = attribution.coverage(rows, entry)
    assert report["attributable_lines"] == 2
    assert report["linked_lines"] == 1
    assert report["coverage"] == 0.5


def test_attribution_is_clinical_roles_only(client_p1, clinician, patient):
    entry_id = post_audio(client_p1, clinician).json()["entry"]["id"]
    assert (
        client_p1.get(f"/entries/{entry_id}/attribution", headers=patient).status_code
        == 403
    )


# --------------------------------------------------------------------------
# What the timings can and cannot tell us
# --------------------------------------------------------------------------


def test_overlapping_speech_is_detected_from_timings(client_p1, clinician):
    """Real arithmetic on real timings — not acoustic diarisation (D-047)."""
    body = post_audio(client_p1, clinician).json()
    assert body["capture"]["overlap_segments"] >= 1

    segments = client_p1.get(
        f"/captures/{body['capture']['session_id']}", headers=clinician
    ).json()["segments"]
    flagged = [s for s in segments if s["overlaps_previous"]]
    assert flagged
    for segment in flagged:
        previous = segments[segment["sequence"] - 1]
        assert segment["start_ms"] < previous["end_ms"]


def test_low_confidence_segments_are_flagged_at_the_shared_threshold(
    client_p1, clinician
):
    """Same 0.6 bar the Glance View already uses — one meaning of 'unsure'."""
    body = post_audio(client_p1, clinician).json()
    assert body["capture"]["low_confidence_segments"] >= 1

    segments = client_p1.get(
        f"/captures/{body['capture']['session_id']}", headers=clinician
    ).json()["segments"]
    for segment in segments:
        expected = segment["confidence"] < capture.LOW_CONFIDENCE
        assert segment["low_confidence"] is expected


def test_code_switched_speech_keeps_its_language_tag(client_p1, clinician):
    body = post_audio(client_p1, clinician).json()
    assert "en-ms" in body["capture"]["languages"]

    segments = client_p1.get(
        f"/captures/{body['capture']['session_id']}", headers=clinician
    ).json()["segments"]
    switched = [s for s in segments if s["language"] == "en-ms"]
    assert switched
    # The non-English words survive redaction and summarisation intact.
    assert any("sakit" in s["text"] or "pecah" in s["text"] for s in switched)


# --------------------------------------------------------------------------
# Transcript parsing
# --------------------------------------------------------------------------


def test_parses_json_and_plain_text_transcripts():
    rows = json.dumps(
        [
            {"speaker": "doctor", "text": "How is the ankle?", "start_ms": 0,
             "end_ms": 2000, "confidence": 0.9, "language": "en"},
            {"speaker": "patient", "text": "Still swollen.", "start_ms": 2000,
             "end_ms": 3500, "confidence": 0.4, "language": "en"},
        ]
    )
    turns = capture.parse_transcript(rows)
    assert [t.speaker for t in turns] == ["clinician", "patient"]  # doctor -> clinician
    assert turns[1].confidence == 0.4

    text_turns = capture.parse_transcript(
        "[00:05] nurse: Blood pressure is 138 over 86.\npatient: I feel alright."
    )
    assert [t.speaker for t in text_turns] == ["staff", "patient"]  # nurse -> staff
    assert text_turns[0].start_ms == 5000


def test_unknown_speaker_labels_cannot_impersonate_the_system():
    """An uploaded file must not be able to introduce a speaker that reads as a
    system author in the timeline."""
    turns = capture.parse_transcript("Care Note AI: the patient is fine.")
    assert turns[0].speaker == "other"


def test_transcript_without_stated_confidence_is_not_treated_as_certain():
    turns = capture.parse_transcript("patient: my ankle is swollen")
    assert turns[0].confidence < 1.0


def test_interaction_type_derives_from_kind_and_role():
    from app.core.enums import InteractionType

    assert (
        capture.interaction_type_for(CaptureKind.PATIENT, Role.PATIENT)
        is InteractionType.AI_PATIENT_SESSION
    )
    assert (
        capture.interaction_type_for(CaptureKind.CLINICAL, Role.CLINICIAN)
        is InteractionType.DOCTOR_PATIENT_CONSULT
    )
    assert (
        capture.interaction_type_for(CaptureKind.CLINICAL, Role.STAFF)
        is InteractionType.NURSE_PATIENT_CONSULT
    )


# --------------------------------------------------------------------------
# Content safety, on a new write path
# --------------------------------------------------------------------------


def test_script_payload_in_a_transcript_is_stored_as_literal_text(
    client_p1, clinician, seeded_p1
):
    """A new ingestion path is a new chance to break D-015. It does not."""
    payload = "patient: my ankle hurts <script>alert('x')</script> since Monday"
    response = client_p1.post(
        "/patients/patient-a1/capture",
        data={"kind": "clinical", "transcript": payload},
        headers=clinician,
    )
    assert response.status_code == 201

    session_id = response.json()["capture"]["session_id"]
    segment = (
        seeded_p1["db"]
        .query(TranscriptSegment)
        .filter(TranscriptSegment.session_id == session_id)
        .first()
    )
    # Stored verbatim — neither executed nor silently altered.
    assert "<script>alert('x')</script>" in segment.redacted_text


def test_fixture_scribed_notes_also_get_transcript_and_attribution(
    client_p1, clinician
):
    """The Phase 2 scribe path writes segments the same way capture does, so it
    gets the same line-level provenance — the transcript endpoint is keyed on
    the segments, not on a CaptureSession that only a recording has.
    """
    response = client_p1.post(
        "/patients/patient-a1/scribe",
        json={"interaction_type": "doctor_patient_consult"},
        headers=clinician,
    )
    assert response.status_code == 201, response.text
    entry = response.json()
    session_id = entry["ai_session_id"]

    detail = client_p1.get(f"/captures/{session_id}", headers=clinician)
    assert detail.status_code == 200
    body = detail.json()
    # No recording happened, so there is no recording to describe...
    assert body["capture"] is None
    assert "audio" not in body["notice"].lower()
    # ...but the transcript and its provenance are fully there.
    assert body["segments"]

    links = client_p1.get(
        f"/entries/{entry['id']}/attribution", headers=clinician
    ).json()
    assert links
    assert all(link["resolves"] for link in links)


def test_transcript_endpoint_404s_for_an_unknown_session(client_p1, clinician):
    response = client_p1.get("/captures/sess-nope", headers=clinician)
    assert response.status_code == 404


def test_attribution_rows_are_scoped_to_the_entry_version(client_p1, clinician, seeded_p1):
    """Offsets belong to the version they were computed against."""
    entry_id = post_audio(client_p1, clinician).json()["entry"]["id"]
    rows = (
        seeded_p1["db"]
        .query(SummaryAttribution)
        .filter(SummaryAttribution.entry_id == entry_id)
        .all()
    )
    assert rows
    assert all(row.source_version_number == 1 for row in rows)


def test_dangling_pointer_would_be_reported_rather_than_linked(seeded_p1):
    """The failure mode this design is built to avoid, asserted directly."""
    with pytest.raises(ProvenanceError):
        resolve(
            seeded_p1["db"],
            "transcript://cap-does-not-exist#segment:3",
            clinic_id="clinic-a",
        )
