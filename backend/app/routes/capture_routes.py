"""Ambient consult capture (Phase 5): recording in, timeline entry out.

Three routes, and the access rules are the interesting part.

**Who may capture what is decided by the token, not the form.** The brief scopes
patient voice capture to the patient view and clinical voice capture to the
clinical view. That is enforced here rather than by which button a client draws:
a patient login submitting `kind=clinical` is refused, and so is a clinician
submitting `kind=patient`. The resulting entry type is then *derived* from the
authenticated role (`capture.interaction_type_for`), so there is no field on the
request that could enter a recording into the record as a different kind of
encounter than the one the caller was actually in.

**Transcripts are clinical-roles-only, including a patient's own.** A patient
recording their consultation captures the clinician's half of it too, so serving
that transcript back to the patient view would route straight around the
patient-facing filter that the rest of this build enforces carefully. Patients
get a receipt for what they submitted, not a transcript. See DECISIONS.md D-049.

**Audio is accepted and not kept.** It is read into memory, transcribed, and
dropped. `GET /captures/{id}` reports the byte count that arrived and
`audio_retained: false` beside it, so the claim is checkable in the product
rather than only in the README.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.ai.asr_client import AudioEgressBlocked, UnsupportedAudio
from app.core.audit_logging import log_event
from app.core.enums import AI_SCRIBED_TYPES, CaptureKind, CaptureSource, EntryType, Role
from app.core.provenance import ProvenanceError, resolve
from app.core.sanitization import ContentTooLongError
from app.core.timeutil import iso_utc
from app.models import CaptureSession, Entry, Patient, SummaryAttribution, TranscriptSegment
from app.routes.schemas import EntryOut, entry_out
from app.security.rbac import AccessScope, require_access
from app.services import attribution, capture

router = APIRouter(tags=["capture"])

# Admin is absent by design: oversight, not authorship (D-011). The dependency
# refuses it before any handler code runs, which is stronger than a check inside
# the body that a later edit could reorder.
CAPTURE_ROLES = (Role.PATIENT, Role.STAFF, Role.CLINICIAN)
TRANSCRIPT_ROLES = (Role.STAFF, Role.CLINICIAN, Role.ADMIN)


# --------------------------------------------------------------------------
# Wire formats
# --------------------------------------------------------------------------


class SegmentOut(BaseModel):
    """One spoken segment, already redacted before it was ever stored."""

    sequence: int
    speaker_label: str
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None
    language: str | None
    low_confidence: bool
    overlaps_previous: bool
    provenance_pointer: str


class CaptureOut(BaseModel):
    session_id: str
    kind: str
    source: str
    entry_id: str | None
    patient_id: str
    asr_provider: str
    asr_model: str
    transcription_simulated: bool
    audio_bytes_received: int
    audio_retained: bool
    duration_ms: int
    duration_source: str
    segment_count: int
    languages: list[str]
    mean_confidence: float | None
    low_confidence_segments: int
    overlap_segments: int
    redaction_count: int
    device_label: str | None
    created_by_role: str
    created_at: str


class AttributionOut(BaseModel):
    """A line of a summary, and the spoken words behind it."""

    span_start: int
    span_end: int
    span_text: str
    source_version_number: int
    segment_sequence: int
    provenance_pointer: str
    match_type: str
    match_score: float
    speaker_label: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    segment_text: str | None = None
    segment_confidence: float | None = None
    resolves: bool = False


class CaptureResult(BaseModel):
    """What the client gets back after a capture completes.

    `entry` is present only for clinical roles. A patient gets the receipt and
    the confirmation line; the note itself goes to their care team.
    """

    capture: CaptureOut
    entry: EntryOut | None = None
    attribution_coverage: dict[str, Any] | None = None
    message: str


def _capture_out(row: CaptureSession) -> CaptureOut:
    return CaptureOut(
        session_id=row.session_id,
        kind=row.kind,
        source=row.source,
        entry_id=row.entry_id,
        patient_id=row.patient_id,
        asr_provider=row.asr_provider,
        asr_model=row.asr_model,
        transcription_simulated=bool(row.transcription_simulated),
        audio_bytes_received=row.audio_bytes_received,
        audio_retained=bool(row.audio_retained),
        duration_ms=row.duration_ms,
        # Whether the number came from the browser or from a byte-count guess.
        # A duration shown without saying which is a measurement claim we did
        # not earn (see asr_client.estimate_duration_ms).
        duration_source=(
            "transcript" if row.source == str(CaptureSource.TRANSCRIPT_UPLOAD)
            else "client_measured" if row.source == str(CaptureSource.LIVE_RECORDING)
            else "estimated_from_bytes"
        ),
        segment_count=row.segment_count,
        languages=json.loads(row.languages or "[]"),
        mean_confidence=row.mean_confidence,
        low_confidence_segments=row.low_confidence_segments,
        overlap_segments=row.overlap_segments,
        redaction_count=row.redaction_count,
        device_label=row.device_label,
        created_by_role=str(row.created_by_role),
        created_at=iso_utc(row.created_at),
    )


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------


@router.post(
    "/patients/{patient_id}/capture",
    response_model=CaptureResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_capture(
    patient_id: str,
    kind: CaptureKind = Form(...),
    source: CaptureSource = Form(CaptureSource.AUDIO_UPLOAD),
    audio: UploadFile | None = File(default=None),
    transcript: str | None = Form(default=None),
    transcript_file: UploadFile | None = File(default=None),
    duration_ms: int | None = Form(default=None),
    device_label: str | None = Form(default=None),
    scope: AccessScope = Depends(require_access(*CAPTURE_ROLES)),
) -> CaptureResult:
    """Submit a recording or a transcript and get back a scribed entry.

    Accepts three shapes so the minimum-viable path never depends on a working
    microphone: an audio file, an uploaded transcript file, or transcript text
    pasted into the form.
    """
    scope.assert_patient_visible(patient_id)
    patient = scope.get_or_404(Patient, patient_id)

    # The view boundary from the brief, enforced server-side. A patient may only
    # produce a patient capture; a clinical user may only produce a clinical one.
    # Each message names the capture that was refused, not the caller's own
    # view — "you asked for X and X is not available here" is actionable;
    # "you are a patient" is a fact they already knew.
    if scope.role is Role.PATIENT and kind is not CaptureKind.PATIENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Clinical voice capture is available only in the clinical view",
        )
    if scope.role in (Role.STAFF, Role.CLINICIAN) and kind is not CaptureKind.CLINICAL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Patient voice capture is available only in the patient view",
        )

    audio_bytes: bytes | None = None
    audio_mime: str | None = None
    transcript_text: str | None = None

    if audio is not None and (audio.filename or "").strip():
        audio_bytes = await audio.read()
        audio_mime = audio.content_type
    elif transcript_file is not None and (transcript_file.filename or "").strip():
        raw = await transcript_file.read()
        try:
            transcript_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Transcript file must be UTF-8 text",
            ) from None
        source = CaptureSource.TRANSCRIPT_UPLOAD
    elif transcript and transcript.strip():
        transcript_text = transcript
        source = CaptureSource.TRANSCRIPT_UPLOAD

    if audio_bytes is None and not transcript_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide an audio recording, a transcript file, or transcript text",
        )
    # An audio payload that did not arrive from the recorder is an upload,
    # whatever the form said — the source field describes provenance and a
    # client should not be able to relabel it.
    if audio_bytes is not None and source is CaptureSource.TRANSCRIPT_UPLOAD:
        source = CaptureSource.AUDIO_UPLOAD

    try:
        entry, capture_row = capture.run_capture(
            scope.db,
            patient=patient,
            kind=kind,
            source=source,
            actor_id=scope.user_id,
            actor_role=scope.role,
            audio=audio_bytes,
            audio_mime=audio_mime,
            transcript_text=transcript_text,
            client_duration_ms=duration_ms,
            device_label=device_label,
        )
    except AudioEgressBlocked as exc:
        # 502: the request was legitimate; the configured recogniser is one this
        # deployment refuses to send un-redacted audio to.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    except (UnsupportedAudio, capture.TranscriptParseError, ContentTooLongError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)
        ) from exc

    # A patient sees a receipt, not the clinical note that was written from
    # their recording — the same boundary the timeline already enforces.
    if scope.role is Role.PATIENT:
        return CaptureResult(
            capture=_capture_out(capture_row),
            entry=None,
            message=(
                "Sent to your care team. Your name and contact details were removed "
                "before anything was processed, and the recording itself was not kept."
            ),
        )

    links = (
        scope.query(SummaryAttribution)
        .filter(
            SummaryAttribution.entry_id == entry.id,
            SummaryAttribution.source_version_number == entry.version_number,
        )
        .all()
    )
    return CaptureResult(
        capture=_capture_out(capture_row),
        entry=entry_out(entry, author_name="Care Note AI", ai_note=entry.ai_note),
        attribution_coverage=attribution.coverage(links, entry),
        message="Consult captured. Review the summary before relying on it.",
    )


# --------------------------------------------------------------------------
# Reading a capture back
# --------------------------------------------------------------------------


@router.get("/patients/{patient_id}/captures", response_model=list[CaptureOut])
def list_captures(
    patient_id: str, scope: AccessScope = Depends(require_access(*TRANSCRIPT_ROLES))
) -> list[CaptureOut]:
    """Captures for one patient, newest first. Clinical roles only."""
    scope.get_or_404(Patient, patient_id)
    rows = (
        scope.query(CaptureSession)
        .filter(CaptureSession.patient_id == patient_id)
        .order_by(CaptureSession.created_at.desc())
        .all()
    )
    return [_capture_out(row) for row in rows]


@router.get("/captures/{session_id}")
def get_capture(
    session_id: str, scope: AccessScope = Depends(require_access(*TRANSCRIPT_ROLES))
) -> dict[str, Any]:
    """The speaker-labelled transcript behind an AI-scribed note.

    Keyed on the SEGMENTS, not on the capture row. Every AI-scribed note has a
    transcript behind it — the Phase 2 fixture path writes segments exactly the
    same way voice capture does — but only a recording has a `CaptureSession`
    with a duration, a recogniser and a byte count. Requiring the capture row
    would have made this endpoint report "no transcript" for notes whose
    transcript is sitting right there.

    Patient logins are refused by the dependency, including for their own
    recording (D-049): a consult recorded in the patient view still contains
    the clinician's half of the conversation.
    """
    segments = (
        scope.query(TranscriptSegment)
        .filter(TranscriptSegment.session_id == session_id)
        .order_by(TranscriptSegment.sequence)
        .all()
    )
    if not segments:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No transcript is stored for this session",
        )

    row = (
        scope.query(CaptureSession)
        .filter(CaptureSession.session_id == session_id)
        .first()
    )

    previous_end = 0
    out: list[SegmentOut] = []
    for segment in segments:
        out.append(
            SegmentOut(
                sequence=segment.sequence,
                speaker_label=segment.speaker_label,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.redacted_text,
                confidence=segment.confidence,
                language=segment.language,
                low_confidence=(
                    segment.confidence is not None
                    and segment.confidence < capture.LOW_CONFIDENCE
                ),
                overlaps_previous=bool(out) and segment.start_ms < previous_end,
                provenance_pointer=f"transcript://{session_id}#segment:{segment.sequence}",
            )
        )
        previous_end = max(previous_end, segment.end_ms)

    log_event(
        actor_id=scope.user_id,
        action="capture.read",
        target_type="capture",
        target_id=session_id,
        clinic_id=scope.clinic_id,
        metadata={"role": str(scope.role), "segments": len(out)},
    )

    simulated = bool(row and row.transcription_simulated)
    return {
        # Null for a fixture-generated session: there was no recording, so
        # there is no recording to describe. The client renders the transcript
        # either way and simply omits the capture header.
        "capture": _capture_out(row).model_dump() if row else None,
        "segments": [segment.model_dump() for segment in out],
        # Said in the payload, not only in the docs, because this is the fact a
        # reviewer most needs and is least able to verify from the outside.
        "notice": (
            "Segments are stored already redacted."
            + (" The audio was not retained." if row else "")
            + (
                " This transcript was produced by a simulated recogniser, not by "
                "speech recognition."
                if simulated
                else ""
            )
        ),
    }


@router.get("/entries/{entry_id}/attribution", response_model=list[AttributionOut])
def get_attribution(
    entry_id: str, scope: AccessScope = Depends(require_access(*TRANSCRIPT_ROLES))
) -> list[AttributionOut]:
    """Which line of this summary came from which spoken segment.

    Every pointer returned is resolved before it is handed over, so a dangling
    one surfaces as `resolves: false` rather than as a link that fails when a
    clinician clicks it mid-consult.
    """
    entry = scope.get_or_404(Entry, entry_id)
    scope.assert_can_view_type(entry.type)
    if EntryType(entry.type) not in AI_SCRIBED_TYPES:
        return []

    rows = (
        scope.query(SummaryAttribution)
        .filter(
            SummaryAttribution.entry_id == entry_id,
            SummaryAttribution.source_version_number == entry.version_number,
        )
        .order_by(SummaryAttribution.span_start)
        .all()
    )

    results: list[AttributionOut] = []
    for row in rows:
        item = AttributionOut(
            span_start=row.span_start,
            span_end=row.span_end,
            span_text=entry.content[row.span_start : row.span_end],
            source_version_number=row.source_version_number,
            segment_sequence=row.segment_sequence,
            provenance_pointer=row.provenance_pointer,
            match_type=row.match_type,
            match_score=row.match_score,
        )
        try:
            resolved = resolve(
                scope.db, row.provenance_pointer, clinic_id=scope.clinic_id
            )
        except ProvenanceError:
            results.append(item)
            continue
        item.resolves = True
        item.speaker_label = resolved.get("speaker_label")
        item.start_ms = resolved.get("start_ms")
        item.end_ms = resolved.get("end_ms")
        item.segment_text = resolved.get("text")
        segment = (
            scope.query(TranscriptSegment)
            .filter(
                TranscriptSegment.session_id == row.session_id,
                TranscriptSegment.sequence == row.segment_sequence,
            )
            .first()
        )
        item.segment_confidence = getattr(segment, "confidence", None)
        results.append(item)
    return results
