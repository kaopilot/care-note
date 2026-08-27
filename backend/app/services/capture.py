"""Ambient consult capture — audio or transcript in, timeline entry out.

This module deliberately adds no summarisation, no redaction and no storage of
its own. It converts a recording (or an uploaded transcript) into the
turn-structured shape `services/scribe.py` has consumed since Phase 2, hands it
over, and records what happened around the edges.

    audio ──► asr_client.transcribe ─┐
                                     ├─► turns ──► scribe.run_scribe ──► Entry
    transcript upload ──► parse ─────┘                 │                + AIScribedNote
                                                       │                + TranscriptSegment
                                                       └─► redact_phi ──► LLM

That the two sources converge on one pipeline is the whole reason Phase 2 wrote
its fixtures as `Turn` objects with speaker labels and timings rather than as
flat text. Voice capture changes where the words come from and nothing else:
redaction, summarisation, provenance, highlight generation, versioning and RBAC
all behave exactly as they already did, and are already tested.

Three things this module owns
-----------------------------
1. **Parsing an uploaded transcript** into turns, forgivingly enough to be
   usable and strictly enough to reject junk.
2. **Reading the timings** for signals the audio would otherwise carry: who
   spoke over whom, how confident the recogniser was, which languages appeared.
3. **Recording the capture itself** — including how many bytes of audio arrived
   and that none of them were kept.
"""

from __future__ import annotations

import json
import re
import uuid

from sqlalchemy.orm import Session

from app.ai import asr_client
from app.core.audit_logging import log_event
from app.core.enums import CaptureKind, CaptureSource, InteractionType, Role
from app.core.sanitization import ContentTooLongError, prepare_content
from app.models import CaptureSession, Entry, Patient
from app.services import attribution, scribe
from app.services.transcripts import Turn

# Confidence below which a segment is called out for checking. Same threshold as
# the Glance View's AI-confidence flag, on purpose — one number, one meaning of
# "the machine is unsure", rather than a second visual language for the same
# idea (Phase 5 brief note, and Phase 2.4's ConfidenceChip).
LOW_CONFIDENCE = 0.6

MAX_TRANSCRIPT_CHARS = 60_000
MAX_TURNS = 400

# Speakers a transcript may name. Anything else is mapped to `other` rather than
# stored, so an uploaded file cannot introduce a speaker role that looks like a
# system author in the timeline.
KNOWN_SPEAKERS = frozenset({"clinician", "doctor", "staff", "nurse", "patient", "system"})
_SPEAKER_ALIASES = {"doctor": "clinician", "nurse": "staff"}

# "patient: text", "[00:12] patient: text", "00:12 patient - text"
_LINE_RE = re.compile(
    r"^\s*(?:[\[(]?(?P<ts>\d{1,2}:\d{2}(?::\d{2})?(?:\.\d{1,3})?)[\])]?\s*)?"
    r"(?P<speaker>[A-Za-z][A-Za-z _-]{0,30}?)\s*[:\-–]\s*(?P<text>\S.*)$"
)


class TranscriptParseError(ValueError):
    """Raised when an uploaded transcript cannot be read as turns."""


# --------------------------------------------------------------------------
# Interaction type is derived from the caller, never from the request
# --------------------------------------------------------------------------


def interaction_type_for(kind: CaptureKind, role: Role) -> InteractionType:
    """Which kind of AI-scribed note this capture becomes.

    Derived from the authenticated role, never accepted from the body. A staff
    login recording a consult produces a nurse consult summary; a clinician
    produces a doctor consult summary; a patient produces a patient session
    summary. There is no field a client could set to have their recording
    entered as a different kind of encounter than the one they were in.
    """
    if kind is CaptureKind.PATIENT:
        return InteractionType.AI_PATIENT_SESSION
    if role is Role.CLINICIAN:
        return InteractionType.DOCTOR_PATIENT_CONSULT
    return InteractionType.NURSE_PATIENT_CONSULT


# --------------------------------------------------------------------------
# Transcript parsing
# --------------------------------------------------------------------------


def _timestamp_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = parts
        elif len(parts) == 2:
            hours, minutes, seconds = "0", parts[0], parts[1]
        else:
            return None
        return int(
            (int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000
        )
    except ValueError:
        return None


def _normalise_speaker(raw: str) -> str:
    speaker = re.sub(r"[^a-z]", "", (raw or "").strip().lower())
    if speaker in _SPEAKER_ALIASES:
        speaker = _SPEAKER_ALIASES[speaker]
    return speaker if speaker in KNOWN_SPEAKERS else "other"


def _coerce_confidence(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        # An uploaded transcript that states no confidence is not a confident
        # transcript. 0.75 rather than 1.0, so the UI never shows certainty
        # that nothing measured.
        return 0.75
    return round(max(0.0, min(1.0, number)), 2)


def parse_transcript(raw: str) -> list[Turn]:
    """Turns from either a JSON array or speaker-labelled plain text.

    JSON is the richer form and is tried first — it can carry per-turn timings,
    confidence and language, which is what a real recogniser emits. Plain text
    is the form a human will actually paste, so it is supported too, with
    timings synthesised at a reading-speed estimate and marked as such by
    carrying no recogniser confidence of their own.
    """
    cleaned, markers = prepare_content(raw)
    if len(cleaned) > MAX_TRANSCRIPT_CHARS:
        raise ContentTooLongError(
            f"transcript is {len(cleaned)} characters; maximum is {MAX_TRANSCRIPT_CHARS}"
        )
    if not cleaned.strip():
        raise TranscriptParseError("Transcript is empty")

    turns = _parse_json_transcript(cleaned)
    if turns is None:
        turns = _parse_text_transcript(cleaned)
    if not turns:
        raise TranscriptParseError(
            "Could not read any turns. Use lines like 'patient: my ankle is swollen', "
            "or a JSON array of {speaker, text, start_ms, end_ms, confidence, language}."
        )
    if len(turns) > MAX_TURNS:
        raise TranscriptParseError(f"Transcript has more than {MAX_TURNS} turns")
    # Markers travel to the audit log via the caller; content is stored verbatim.
    parse_transcript.last_markers = markers  # type: ignore[attr-defined]
    return turns


def _parse_json_transcript(text: str) -> list[Turn] | None:
    stripped = text.strip()
    if not stripped.startswith("["):
        return None
    try:
        rows = json.loads(stripped)
    except ValueError as exc:
        raise TranscriptParseError(f"Transcript looks like JSON but will not parse: {exc}")
    if not isinstance(rows, list):
        raise TranscriptParseError("JSON transcript must be an array of turns")

    turns: list[Turn] = []
    cursor = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TranscriptParseError(f"Turn {index} is not an object")
        body = str(row.get("text") or "").strip()
        if not body:
            continue
        start = row.get("start_ms")
        end = row.get("end_ms")
        start_ms = int(start) if isinstance(start, (int, float)) else cursor
        end_ms = (
            int(end)
            if isinstance(end, (int, float)) and int(end) > start_ms
            else start_ms + _reading_time_ms(body)
        )
        cursor = end_ms
        turns.append(
            Turn(
                speaker=_normalise_speaker(str(row.get("speaker") or "other")),
                text=body,
                start_ms=start_ms,
                end_ms=end_ms,
                confidence=_coerce_confidence(row.get("confidence")),
                language=str(row.get("language") or "en")[:20],
            )
        )
    return turns


def _reading_time_ms(text: str) -> int:
    """Speaking time at roughly 165 words per minute, floored at a second."""
    words = max(1, len(text.split()))
    return max(1000, int(words / 165 * 60_000))


def _parse_text_transcript(text: str) -> list[Turn]:
    turns: list[Turn] = []
    cursor = 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        match = _LINE_RE.match(line)
        if not match:
            # An unlabelled line continues the previous speaker — which is what
            # a wrapped paragraph in a pasted transcript actually is.
            if turns:
                previous = turns[-1]
                merged = f"{previous.text} {line.strip()}"
                turns[-1] = Turn(
                    previous.speaker,
                    merged,
                    previous.start_ms,
                    previous.start_ms + _reading_time_ms(merged),
                    previous.confidence,
                    previous.language,
                )
                cursor = turns[-1].end_ms
            continue

        body = match.group("text").strip()
        if not body:
            continue
        start_ms = _timestamp_to_ms(match.group("ts"))
        if start_ms is None:
            start_ms = cursor
        end_ms = start_ms + _reading_time_ms(body)
        cursor = end_ms
        turns.append(
            Turn(
                speaker=_normalise_speaker(match.group("speaker")),
                text=body,
                start_ms=start_ms,
                end_ms=end_ms,
                confidence=0.75,
                language="en",
            )
        )
    return turns


# --------------------------------------------------------------------------
# What the timings can tell us
# --------------------------------------------------------------------------


def analyse(turns: list[Turn]) -> dict:
    """Signals derivable from the turn structure alone.

    Overlap is real arithmetic on real timings: a segment that starts before the
    previous one ended is two people talking at once, and that is worth flagging
    because it is where recognisers make their worst mistakes. It is NOT
    acoustic diarisation — nothing here separates voices from a mixed waveform.
    See DECISIONS.md D-047; the README says the same thing in the gap list.
    """
    if not turns:
        return {
            "segment_count": 0,
            "duration_ms": 0,
            "languages": [],
            "mean_confidence": None,
            "low_confidence_segments": 0,
            "overlap_segments": 0,
            "speakers": [],
        }

    confidences = [turn.confidence for turn in turns if turn.confidence is not None]
    overlaps = sum(
        1
        for previous, current in zip(turns, turns[1:])
        if current.start_ms < previous.end_ms
    )
    return {
        "segment_count": len(turns),
        "duration_ms": max(turn.end_ms for turn in turns),
        "languages": sorted({turn.language for turn in turns if turn.language}),
        "mean_confidence": (
            round(sum(confidences) / len(confidences), 2) if confidences else None
        ),
        "low_confidence_segments": sum(
            1 for value in confidences if value < LOW_CONFIDENCE
        ),
        "overlap_segments": overlaps,
        "speakers": sorted({turn.speaker for turn in turns}),
    }


# --------------------------------------------------------------------------
# The capture itself
# --------------------------------------------------------------------------


def run_capture(
    db: Session,
    *,
    patient: Patient,
    kind: CaptureKind,
    source: CaptureSource,
    actor_id: str,
    actor_role: Role,
    audio: bytes | None = None,
    audio_mime: str | None = None,
    transcript_text: str | None = None,
    client_duration_ms: int | None = None,
    device_label: str | None = None,
) -> tuple[Entry, CaptureSession]:
    """One capture, end to end. Returns the created entry and its capture row.

    Exactly one of `audio` or `transcript_text` is used; the route validates
    that one was supplied.
    """
    session_id = f"cap-{patient.id}-{uuid.uuid4().hex[:8]}"
    interaction_type = interaction_type_for(kind, actor_role)
    markers: list[str] = []

    if audio:
        transcription = asr_client.transcribe(
            audio,
            mime=audio_mime,
            kind=str(kind),
            patient_name=patient.name,
            duration_ms=client_duration_ms,
            actor_id=actor_id,
            clinic_id=patient.clinic_id,
        )
        turns = transcription.turns
        asr_provider = transcription.provider
        asr_model = transcription.model
        simulated = transcription.simulated
        audio_bytes = len(audio)
    else:
        turns = parse_transcript(transcript_text or "")
        markers = list(getattr(parse_transcript, "last_markers", []) or [])
        # Nothing was recognised, so no recogniser is credited. Claiming a model
        # transcribed text that arrived as text would be a provenance lie.
        asr_provider = "none"
        asr_model = "transcript-upload"
        simulated = False
        audio_bytes = 0

    stats = analyse(turns)

    # Written before the scribe runs so it is committed by the same transaction
    # that writes the entry. A capture row with no entry is a visible, harmless
    # failure; an entry with no capture row would be a note whose origin the
    # record cannot account for.
    capture = CaptureSession(
        session_id=session_id,
        clinic_id=patient.clinic_id,
        patient_id=patient.id,
        kind=str(kind),
        source=str(source),
        asr_provider=asr_provider,
        asr_model=asr_model,
        transcription_simulated=simulated,
        audio_bytes_received=audio_bytes,
        # Never true. The column exists so the claim is a stored fact that a
        # test can assert against, rather than a sentence in a README.
        audio_retained=False,
        audio_mime=audio_mime,
        duration_ms=client_duration_ms or stats["duration_ms"],
        segment_count=stats["segment_count"],
        languages=json.dumps(stats["languages"]),
        mean_confidence=stats["mean_confidence"],
        low_confidence_segments=stats["low_confidence_segments"],
        overlap_segments=stats["overlap_segments"],
        device_label=(device_label or "")[:120] or None,
        created_by=actor_id,
        created_by_role=actor_role,
    )
    db.add(capture)
    db.flush()

    # The unchanged Phase 2 pipeline: redaction per turn, segments stored already
    # redacted, llm_client re-redacting before egress, structured summary,
    # entry + AIScribedNote + highlights.
    entry = scribe.run_scribe(
        db,
        patient=patient,
        interaction_type=interaction_type,
        turns=turns,
        actor_id=actor_id,
        session_id=session_id,
    )

    capture.entry_id = entry.id
    ai_note = entry.ai_note
    capture.redaction_count = getattr(ai_note, "redaction_count", 0) or 0

    links = attribution.link_summary_to_segments(db, entry=entry, session_id=session_id)
    db.commit()
    db.refresh(entry)
    db.refresh(capture)

    log_event(
        actor_id=actor_id,
        action="capture.complete",
        target_type="capture",
        target_id=session_id,
        clinic_id=patient.clinic_id,
        metadata={
            "kind": str(kind),
            "source": str(source),
            "interaction_type": str(interaction_type),
            "segments": stats["segment_count"],
            "low_confidence": stats["low_confidence_segments"],
            "overlaps": stats["overlap_segments"],
            "languages": ",".join(stats["languages"]),
            "asr_provider": asr_provider,
            "simulated": simulated,
            "audio_bytes": audio_bytes,
            "audio_retained": False,
            "attributions": len(links),
            "injection_markers": len(markers),
        },
    )
    return entry, capture
