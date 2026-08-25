"""Provenance pointers.

Every highlight and every AI-scribed entry must be traceable to a source. A
pointer is a string URI rather than a foreign key, because the targets are not
all rows of one table: sometimes a whole entry, sometimes a character span
inside one, sometimes a turn in an AI session or a segment of a transcript.

Grammar
-------
    entry://<entry_id>
    entry://<entry_id>#span:<start>-<end>
    session://<session_id>
    session://<session_id>#turn:<n>
    transcript://<session_id>#segment:<sequence>

Anything that stores a pointer must be resolvable by `resolve()`; the
`test_highlight_provenance.py` test in Phase 3 asserts exactly that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

_POINTER_RE = re.compile(
    r"^(?P<scheme>entry|session|transcript)://(?P<target>[^#]+)"
    r"(?:#(?P<frag_kind>span|turn|segment):(?P<frag_value>[0-9]+(?:-[0-9]+)?))?$"
)


class ProvenanceError(ValueError):
    """Raised when a pointer is malformed or cannot be resolved."""


@dataclass(frozen=True)
class ProvenancePointer:
    scheme: str
    target_id: str
    fragment_kind: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    index: int | None = None

    def __str__(self) -> str:
        base = f"{self.scheme}://{self.target_id}"
        if self.fragment_kind == "span":
            return f"{base}#span:{self.span_start}-{self.span_end}"
        if self.fragment_kind in {"turn", "segment"}:
            return f"{base}#{self.fragment_kind}:{self.index}"
        return base


def entry_pointer(entry_id: str, start: int | None = None, end: int | None = None) -> str:
    if start is None or end is None:
        return str(ProvenancePointer("entry", entry_id))
    return str(ProvenancePointer("entry", entry_id, "span", span_start=start, span_end=end))


def session_pointer(session_id: str, turn: int | None = None) -> str:
    if turn is None:
        return str(ProvenancePointer("session", session_id))
    return str(ProvenancePointer("session", session_id, "turn", index=turn))


def transcript_pointer(session_id: str, segment: int) -> str:
    return str(ProvenancePointer("transcript", session_id, "segment", index=segment))


def parse(pointer: str) -> ProvenancePointer:
    match = _POINTER_RE.match(pointer or "")
    if not match:
        raise ProvenanceError(f"malformed provenance pointer: {pointer!r}")

    scheme = match.group("scheme")
    target = match.group("target")
    kind = match.group("frag_kind")
    value = match.group("frag_value")

    if kind == "span":
        if "-" not in (value or ""):
            raise ProvenanceError(f"span fragment needs start-end: {pointer!r}")
        start_s, end_s = value.split("-", 1)
        start, end = int(start_s), int(end_s)
        if end < start:
            raise ProvenanceError(f"span end precedes start: {pointer!r}")
        return ProvenancePointer(scheme, target, "span", span_start=start, span_end=end)

    if kind in {"turn", "segment"}:
        return ProvenancePointer(scheme, target, kind, index=int(value))

    return ProvenancePointer(scheme, target)


def resolve(db: Session, pointer: str, *, clinic_id: str | None = None) -> dict[str, Any]:
    """Resolve a pointer to its source. Raises ProvenanceError if it dangles.

    `clinic_id`, when given, is enforced: a pointer must never be usable to read
    across a clinic boundary, even if the pointer string itself is valid.
    """
    from app.models import Entry, TranscriptSegment  # local import avoids a cycle

    parsed = parse(pointer)

    if parsed.scheme == "entry":
        entry = db.get(Entry, parsed.target_id)
        if entry is None:
            raise ProvenanceError(f"pointer targets a missing entry: {pointer!r}")
        if clinic_id is not None and entry.clinic_id != clinic_id:
            raise ProvenanceError("pointer crosses a clinic boundary")
        result: dict[str, Any] = {
            "kind": "entry",
            "entry_id": entry.id,
            "entry_type": entry.type,
            "author_role": entry.author_role,
            "timestamp": entry.timestamp,
        }
        if parsed.fragment_kind == "span":
            content = entry.content or ""
            if parsed.span_end > len(content):
                raise ProvenanceError(
                    f"span {parsed.span_start}-{parsed.span_end} exceeds entry length "
                    f"{len(content)}"
                )
            result["span"] = (parsed.span_start, parsed.span_end)
            result["span_text"] = content[parsed.span_start : parsed.span_end]
        return result

    if parsed.scheme == "session":
        from app.models import AIScribedNote

        query = db.query(AIScribedNote).filter(AIScribedNote.session_id == parsed.target_id)
        if clinic_id is not None:
            query = query.filter(AIScribedNote.clinic_id == clinic_id)
        note = query.first()
        if note is None:
            raise ProvenanceError(f"pointer targets an unknown session: {pointer!r}")
        return {
            "kind": "session",
            "session_id": note.session_id,
            "entry_id": note.entry_id,
            "interaction_type": note.interaction_type,
            "model_used": note.model_used,
            "turn": parsed.index,
        }

    # transcript://
    query = db.query(TranscriptSegment).filter(
        TranscriptSegment.session_id == parsed.target_id,
        TranscriptSegment.sequence == parsed.index,
    )
    if clinic_id is not None:
        query = query.filter(TranscriptSegment.clinic_id == clinic_id)
    segment = query.first()
    if segment is None:
        raise ProvenanceError(f"pointer targets a missing transcript segment: {pointer!r}")
    return {
        "kind": "transcript_segment",
        "session_id": segment.session_id,
        "sequence": segment.sequence,
        "speaker_label": segment.speaker_label,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "text": segment.redacted_text,
    }
