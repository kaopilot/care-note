"""Vocabulary of the system. Every role / type string lives here exactly once."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    PATIENT = "patient"
    STAFF = "staff"
    CLINICIAN = "clinician"
    ADMIN = "admin"
    SYSTEM = "system"  # author_role for AI-scribed notes and system events


class EntryType(StrEnum):
    # --- human-authored ---
    PATIENT_NOTE = "patient_note"              # patient-contributed insight
    STAFF_NOTE = "staff_note"                  # staff-owned
    CLINICIAN_SECTION = "clinician_section"    # clinician-owned (plan, assessment)
    PATIENT_INSTRUCTION = "patient_instruction"  # clinician -> patient facing
    PATIENT_SUMMARY = "patient_summary"          # patient-facing summary

    # --- AI-scribed (author_role is always Role.SYSTEM) ---
    AI_DOCTOR_CONSULT_SUMMARY = "ai_doctor_consult_summary"
    AI_NURSE_CONSULT_SUMMARY = "ai_nurse_consult_summary"
    AI_PATIENT_SESSION_SUMMARY = "ai_patient_session_summary"

    # --- machine-emitted, not authored ---
    SYSTEM_EVENT = "system_event"


AI_SCRIBED_TYPES = frozenset(
    {
        EntryType.AI_DOCTOR_CONSULT_SUMMARY,
        EntryType.AI_NURSE_CONSULT_SUMMARY,
        EntryType.AI_PATIENT_SESSION_SUMMARY,
    }
)

PATIENT_FACING_TYPES = frozenset(
    {EntryType.PATIENT_SUMMARY, EntryType.PATIENT_INSTRUCTION, EntryType.PATIENT_NOTE}
)


class InteractionType(StrEnum):
    """Source interaction behind an AIScribedNote."""

    DOCTOR_PATIENT_CONSULT = "doctor_patient_consult"
    NURSE_PATIENT_CONSULT = "nurse_patient_consult"
    AI_PATIENT_SESSION = "ai_patient_session"


class CaptureKind(StrEnum):
    """Who recorded an ambient consult capture (Phase 5).

    This is an access dimension, not a label. A `PATIENT` capture may only be
    submitted by a patient login for their own record, and a `CLINICAL` capture
    only by staff or a clinician. The kind is checked against the caller's role
    server-side; it is never trusted from the request body alone.
    """

    PATIENT = "patient"
    CLINICAL = "clinical"


class CaptureSource(StrEnum):
    """How the audio or transcript reached the server.

    Recorded per capture because the three differ in what they can honestly
    claim. A transcript upload was never transcribed by us at all; a live
    recording came from the browser's MediaRecorder; an audio upload is a file
    someone chose. The Glance View never shows this, but the provenance panel
    does — a reviewer should be able to tell which path produced a note.
    """

    LIVE_RECORDING = "live_recording"
    AUDIO_UPLOAD = "audio_upload"
    TRANSCRIPT_UPLOAD = "transcript_upload"


class AttributionMatch(StrEnum):
    """How firmly a summary line traces to a transcript segment (Phase 5).

    `VERBATIM` means the segment's words appear in the summary character for
    character — the pointer is provable, not asserted. `DERIVED` means the line
    and the segment share enough vocabulary to be confident but the wording
    changed, which is what a real model does when it paraphrases. Lines that
    match nothing get no attribution row at all: a provenance system that
    invents a source when it cannot find one is worse than one that admits the
    gap. See DECISIONS.md D-048.
    """

    VERBATIM = "verbatim"
    DERIVED = "derived"


class RiskLevel(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class CommentStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class HighlightStatus(StrEnum):
    SUGGESTED = "suggested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class DecayState(StrEnum):
    """Phase 4 data-decay lifecycle. Every Entry starts HOT."""

    HOT = "hot"        # full content, full Glance View eligibility
    WARM = "warm"      # full content retained, down-weighted in scoring
    COLD = "cold"      # content compressed to a summary, original archived


class InteractionAction(StrEnum):
    """Signals that feed Phase 4 self-learning.

    Recorded is not the same as learned-from. `CREATE` and `VIEW` are written to
    `InteractionLog` for a complete behavioural history but carry weight 0.0 in
    `services/learning.py` — see D-039. Authoring a note is volume, and opening
    a chart is unavoidable; neither is evidence that a clinician stopped and
    paid attention to something.
    """

    VIEW = "view"
    CREATE = "create"
    EDIT = "edit"
    COMMENT = "comment"
    MANUAL_HIGHLIGHT = "manual_highlight"
    PIN = "pin"
    ACCEPT_HIGHLIGHT = "accept_highlight"
    REJECT_HIGHLIGHT = "reject_highlight"
    RESOLVE_COMMENT = "resolve_comment"
