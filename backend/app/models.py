"""SQLAlchemy models — the whole Care Note domain.

Design notes that matter downstream:

* Every clinically-scoped table carries `clinic_id` directly, even where it is
  derivable via a join. Denormalising it is what lets the RBAC layer apply a
  single, uniform clinic filter to any query without knowing the table's shape.
* `Version` stores FULL SNAPSHOTS, not diffs (see DECISIONS.md D-006). Diffs are
  computed on read with difflib; revert is then a pure copy of an old snapshot.
* `provenance_pointer` is a string URI (see app/core/provenance.py), not a
  foreign key, because it must be able to point at things that are not rows in
  this DB — a transcript segment, a span inside an entry, an AI session turn.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.enums import (
    CommentStatus,
    DecayState,
    EntryType,
    HighlightStatus,
    RiskLevel,
    Role,
    TaskStatus,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Tenancy & identity
# --------------------------------------------------------------------------


class Clinic(Base):
    __tablename__ = "clinics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    users: Mapped[list["User"]] = relationship(back_populates="clinic")
    patients: Mapped[list["Patient"]] = relationship(back_populates="clinic")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)
    role: Mapped[Role] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Set only for role=patient: which Patient record this login represents.
    patient_id: Mapped[str | None] = mapped_column(ForeignKey("patients.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    clinic: Mapped[Clinic] = relationship(back_populates="users")


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    dob: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO date, synthetic
    mrn: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    clinic: Mapped[Clinic] = relationship(back_populates="patients")
    entries: Mapped[list["Entry"]] = relationship(back_populates="patient")


# --------------------------------------------------------------------------
# The timeline
# --------------------------------------------------------------------------


class Entry(Base):
    """One unit on the longitudinal timeline. Content here is the CURRENT state;
    history lives in `versions`."""

    __tablename__ = "entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), nullable=False, index=True)
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)

    # --- required metadata on every entry (shared context) ---
    author_role: Mapped[Role] = mapped_column(String(20), nullable=False)
    author_id: Mapped[str] = mapped_column(String(36), nullable=False)  # user id or "system"
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, index=True)
    type: Mapped[EntryType] = mapped_column(String(50), nullable=False, index=True)
    provenance_pointer: Mapped[str | None] = mapped_column(String(500), nullable=True)

    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk_level: Mapped[RiskLevel] = mapped_column(String(20), default=RiskLevel.NONE)

    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    version_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Phase 4 data decay
    decay_state: Mapped[DecayState] = mapped_column(String(20), default=DecayState.HOT)
    decayed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Conflict flagging (shared-context conflict rule; see DECISIONS.md D-007)
    conflict_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    supersedes_entry_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    patient: Mapped[Patient] = relationship(back_populates="entries")
    versions: Mapped[list["Version"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )
    highlights: Mapped[list["Highlight"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )
    ai_note: Mapped["AIScribedNote | None"] = relationship(
        back_populates="entry", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        # The Glance View's hot path: entries for one patient, newest first.
        Index("ix_entries_patient_timestamp", "patient_id", "timestamp"),
        Index("ix_entries_clinic_patient", "clinic_id", "patient_id"),
    )


class Version(Base):
    """Immutable full snapshot of an Entry at one point in time."""

    __tablename__ = "versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entry_id: Mapped[str] = mapped_column(ForeignKey("entries.id"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Full snapshot of the mutable fields at this version.
    content_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    title_snapshot: Mapped[str | None] = mapped_column(String(300), nullable=True)
    risk_level_snapshot: Mapped[str | None] = mapped_column(String(20), nullable=True)

    edited_by: Mapped[str] = mapped_column(String(36), nullable=False)
    edited_by_role: Mapped[Role] = mapped_column(String(20), nullable=False)
    edited_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Set when this version was produced by reverting to an earlier one.
    reverted_from_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    entry: Mapped[Entry] = relationship(back_populates="versions")

    __table_args__ = (UniqueConstraint("entry_id", "version_number", name="uq_entry_version"),)


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entry_id: Mapped[str] = mapped_column(ForeignKey("entries.id"), nullable=False, index=True)
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)
    parent_comment_id: Mapped[str | None] = mapped_column(
        ForeignKey("comments.id"), nullable=True
    )

    author_id: Mapped[str] = mapped_column(String(36), nullable=False)
    author_role: Mapped[Role] = mapped_column(String(20), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON-encoded list of user ids, e.g. '["u-nurse-1"]'
    mentions: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[CommentStatus] = mapped_column(String(20), default=CommentStatus.OPEN)
    # True for staff/clinician/admin threads: never visible to a patient.
    is_internal: Mapped[bool] = mapped_column(Boolean, default=True)

    resolved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    entry: Mapped[Entry] = relationship(back_populates="comments")
    replies: Mapped[list["Comment"]] = relationship()


class Highlight(Base):
    """A candidate for the Glance View, always traceable back to a source span."""

    __tablename__ = "highlights"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entry_id: Mapped[str] = mapped_column(ForeignKey("entries.id"), nullable=False, index=True)
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), nullable=False, index=True)

    # Character offsets into the entry content at the version below.
    span_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    span_end: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    span_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    risk_reason: Mapped[str] = mapped_column(String(300), nullable=False)
    provenance_pointer: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[HighlightStatus] = mapped_column(
        String(20), default=HighlightStatus.SUGGESTED, index=True
    )

    score: Mapped[float] = mapped_column(Float, default=0.0)
    # JSON-encoded score breakdown, e.g. {"recency": .4, "risk": .3, "learned": .2}
    score_breakdown: Mapped[str] = mapped_column(Text, default="{}")
    # JSON-encoded feature tags used by Phase 4 learning, e.g. ["med:warfarin"]
    feature_tags: Mapped[str] = mapped_column(Text, default="[]")

    created_by: Mapped[str] = mapped_column(String(36), nullable=False)  # user id or "system"
    created_by_role: Mapped[Role] = mapped_column(String(20), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    entry: Mapped[Entry] = relationship(back_populates="highlights")


class AIScribedNote(Base):
    """Links an Entry back to the AI session that produced it."""

    __tablename__ = "ai_scribed_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entry_id: Mapped[str] = mapped_column(
        ForeignKey("entries.id"), nullable=False, unique=True, index=True
    )
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)

    session_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    interaction_type: Mapped[str] = mapped_column(String(50), nullable=False)
    model_used: Mapped[str] = mapped_column(String(100), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    # Redaction accounting — proof, per note, that the chokepoint ran.
    redaction_applied: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    redaction_count: Mapped[int] = mapped_column(Integer, default=0)
    # Confidence reported by the summariser, 0..1. Surfaced in the UI so a
    # clinician can calibrate trust rather than being asked to take it on faith.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    entry: Mapped[Entry] = relationship(back_populates="ai_note")


class TranscriptSegment(Base):
    """One diarised segment of a captured consult. Provenance targets for
    Phase 5 voice capture point here."""

    __tablename__ = "transcript_segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker_label: Mapped[str] = mapped_column(String(50), nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_ms: Mapped[int] = mapped_column(Integer, default=0)
    # Stored already-redacted. Raw audio/text never lands in this table.
    redacted_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    language: Mapped[str | None] = mapped_column(String(20), nullable=True)

    __table_args__ = (UniqueConstraint("session_id", "sequence", name="uq_session_sequence"),)


class Task(Base):
    """An open action — what the Glance View's 'needs lab order' row is made of."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), nullable=False, index=True)
    entry_id: Mapped[str | None] = mapped_column(ForeignKey("entries.id"), nullable=True)
    comment_id: Mapped[str | None] = mapped_column(ForeignKey("comments.id"), nullable=True)

    description: Mapped[str] = mapped_column(String(500), nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    assigned_to_role: Mapped[Role | None] = mapped_column(String(20), nullable=True)
    assigned_by: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(String(20), default=TaskStatus.OPEN, index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# --------------------------------------------------------------------------
# Learning & audit
# --------------------------------------------------------------------------


class InteractionLog(Base):
    """Behavioural signal. Feeds Phase 4 adaptive scoring.

    `content_features` holds extracted TAGS ONLY (e.g. ["med:warfarin",
    "section:plan"]) — never the content itself. That is both a privacy
    requirement and what makes the learning signal generalise across entries.
    """

    __tablename__ = "interaction_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_role: Mapped[Role] = mapped_column(String(20), nullable=False)
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)

    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    content_features: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of tags
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class FeatureWeight(Base):
    """Learned importance per feature tag, per clinic. The persisted state of
    Phase 4's self-learning loop. Scoped per clinic so one clinic's habits never
    leak into another's prioritisation."""

    __tablename__ = "feature_weights"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)
    feature_tag: Mapped[str] = mapped_column(String(100), nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    positive_signals: Mapped[int] = mapped_column(Integer, default=0)
    negative_signals: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("clinic_id", "feature_tag", name="uq_clinic_feature"),
    )


class PatientView(Base):
    """When each user last opened each patient — the state behind "what's
    changed since you were last here".

    Two timestamps rather than one, deliberately. `last_viewed_at` moves on
    every page load; if the Glance View compared against it, the act of reading
    the "what's new" group would immediately clear it, and a refresh — or a
    second monitor — would lose the news. `previous_viewed_at` is the stable
    comparison point and only rolls forward when a genuinely new visit begins
    (see VIEW_SESSION_GAP in services/glance.py). See DECISIONS.md D-033.
    """

    __tablename__ = "patient_views"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), nullable=False)
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)

    last_viewed_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    previous_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "patient_id", name="uq_user_patient_view"),
    )


class AuditLog(Base):
    """Who changed what, when. Metadata only — never note content."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    actor_role: Mapped[Role] = mapped_column(String(20), nullable=False)
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)

    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    # JSON metadata: version numbers, field names, counts. No content, ever.
    audit_metadata: Mapped[str] = mapped_column(Text, default="{}")


class EntryArchive(Base):
    """Cold storage for decayed entries (Phase 4). The Entry row survives with a
    compressed summary in `content`; the full original lands here."""

    __tablename__ = "entry_archives"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entry_id: Mapped[str] = mapped_column(ForeignKey("entries.id"), nullable=False, index=True)
    clinic_id: Mapped[str] = mapped_column(ForeignKey("clinics.id"), nullable=False, index=True)
    archived_content: Mapped[str] = mapped_column(Text, nullable=False)
    compression: Mapped[str] = mapped_column(String(20), default="none")
    original_length: Mapped[int] = mapped_column(Integer, default=0)
    archived_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
