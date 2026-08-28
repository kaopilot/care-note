"""Wire formats shared across routes.

`EntryOut` moved here in Phase 2 because five modules now return entries and
each having its own serialiser is how `is_ai_scribed` ends up true in one
response and absent in another. The visual distinction between machine and human
authorship is a hard requirement from the brief; it should be computed in
exactly one place.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.core.enums import AI_SCRIBED_TYPES, EntryType
from app.core.timeutil import UtcDateTime
from app.services import scribe
from app.models import Entry


class EntryOut(BaseModel):
    """One timeline entry.

    Every field the shared context requires on a timeline entry is present and
    non-optional: author_role, author_id, timestamp, type, provenance_pointer.
    `is_ai_scribed` is derived rather than stored so the client cannot disagree
    with the server about which notes are machine output.
    """

    id: str
    patient_id: str
    author_role: str
    author_id: str
    author_name: str | None = None
    timestamp: UtcDateTime
    type: str
    title: str | None
    content: str
    risk_level: str
    provenance_pointer: str | None
    version_number: int
    is_ai_scribed: bool

    # Phase 2 additions. All derived from columns Phase 0 already modelled.
    updated_at: UtcDateTime | None = None
    conflict_flagged: bool = False
    supersedes_entry_id: str | None = None
    decay_state: str = "hot"
    ai_confidence: float | None = None
    ai_confidence_band: str | None = None
    risk_floor_applied: bool = False
    ai_session_id: str | None = None
    ai_model_used: str | None = None
    ai_redaction_count: int | None = None
    comment_count: int = 0
    open_comment_count: int = 0
    highlight_count: int = 0
    editable_by_me: bool = False


def entry_out(
    entry: Entry,
    *,
    author_name: str | None = None,
    ai_note=None,
    comment_count: int = 0,
    open_comment_count: int = 0,
    highlight_count: int = 0,
    editable_by_me: bool = False,
) -> EntryOut:
    return EntryOut(
        id=entry.id,
        patient_id=entry.patient_id,
        author_role=str(entry.author_role),
        author_id=entry.author_id,
        author_name=author_name,
        timestamp=entry.timestamp,
        type=str(entry.type),
        title=entry.title,
        content=entry.content,
        risk_level=str(entry.risk_level),
        provenance_pointer=entry.provenance_pointer,
        version_number=entry.version_number,
        is_ai_scribed=EntryType(entry.type) in AI_SCRIBED_TYPES,
        updated_at=entry.updated_at,
        conflict_flagged=bool(entry.conflict_flagged),
        supersedes_entry_id=entry.supersedes_entry_id,
        decay_state=str(entry.decay_state),
        ai_confidence=getattr(ai_note, "confidence", None),
        ai_confidence_band=(
            scribe.confidence_band(ai_note.confidence) if ai_note is not None else None
        ),
        risk_floor_applied=bool(getattr(ai_note, "risk_floor_applied", False)),
        ai_session_id=getattr(ai_note, "session_id", None),
        ai_model_used=getattr(ai_note, "model_used", None),
        ai_redaction_count=getattr(ai_note, "redaction_count", None),
        comment_count=comment_count,
        open_comment_count=open_comment_count,
        highlight_count=highlight_count,
        editable_by_me=editable_by_me,
    )
