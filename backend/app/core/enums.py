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
