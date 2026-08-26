"""Highlight lifecycle: generate, rescore, decide, and resolve back to source.

A Highlight is a claim the system makes about what matters. The brief's hard
constraint is that every such claim carries a short `risk_reason` and a
`provenance_pointer`, and that a clinician can accept or reject it fast. So
three rules shape this module:

1. **Nothing is surfaced without a reason.** A span with no feature tags
   produces no highlight, even if it scores well on recency. "This is recent"
   is not a reason a clinician can act on.
2. **A human decision is never overwritten by a machine.** Regeneration
   rescores accepted/rejected highlights but never resurrects a rejected one or
   silently drops an accepted one. Re-suggesting something a clinician already
   dismissed is how a system teaches people to ignore it.
3. **Highlights are anchored to the version they were made against.** When the
   underlying entry is edited, the highlight is *stale*, not silently
   re-anchored onto text nobody confirmed. This resolves the open question
   Phase 1 carried forward — see DECISIONS.md D-030.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.enums import AI_SCRIBED_TYPES, EntryType, HighlightStatus, Role
from app.core.provenance import entry_pointer
from app.models import Entry, Highlight, Task, Version
from app.services import features, scoring

# At most this many machine suggestions per entry. The Glance View is a
# 10-second surface; an entry that generates nine candidates has produced noise,
# not signal, and the top few carry almost all the value.
MAX_SUGGESTIONS_PER_ENTRY = 3

# Below this, a candidate is not worth a clinician's attention at all.
MIN_SUGGESTION_SCORE = 0.12


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def _open_task_count(db: Session, entry_id: str) -> int:
    return (
        db.query(Task)
        .filter(Task.entry_id == entry_id)
        .filter(Task.status.in_(["open", "in_progress"]))
        .count()
    )


def refresh_entry_highlights(db: Session, entry: Entry) -> list[Highlight]:
    """Regenerate machine suggestions for one entry, preserving human decisions.

    Called on entry create, edit and after the AI scribe writes a summary — so
    the Glance View reads pre-computed rows rather than scoring the whole
    timeline on the hot path. That is the single biggest reason the Glance View
    meets its latency budget (ARCHITECTURE.md, "Glance View latency").
    """
    entry_tags = features.entry_level_tags(entry.type, entry.risk_level)
    open_tasks = _open_task_count(db, entry.id)

    existing = db.query(Highlight).filter(Highlight.entry_id == entry.id).all()
    decided = {
        (h.span_start, h.span_end)
        for h in existing
        if h.status in (HighlightStatus.ACCEPTED, HighlightStatus.REJECTED)
    }

    candidates: list[tuple[float, dict]] = []
    for start, end, span_text in features.sentences(entry.content or ""):
        span_tags, reasons = features.tag_span(span_text)
        if not reasons:
            continue  # rule 1: no reason, no highlight
        if (start, end) in decided:
            continue  # rule 2: a human already ruled on this span

        tags = sorted(set(span_tags + entry_tags))
        score, breakdown = scoring.score_span(
            db,
            clinic_id=entry.clinic_id,
            timestamp=entry.timestamp,
            risk_level=entry.risk_level,
            tags=tags,
            open_task_count=open_tasks,
            decay_state=entry.decay_state,
        )
        if score < MIN_SUGGESTION_SCORE:
            continue
        candidates.append(
            (
                score,
                {
                    "span_start": start,
                    "span_end": end,
                    "span_text": span_text,
                    "risk_reason": _compose_reason(reasons, entry),
                    "tags": tags,
                    "breakdown": breakdown,
                },
            )
        )

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    keep = candidates[:MAX_SUGGESTIONS_PER_ENTRY]

    # Undecided suggestions from a previous pass are replaced wholesale. They
    # are machine output with no human investment in them, so churning them is
    # free; accepted and rejected rows are never touched here.
    for row in existing:
        if row.status is HighlightStatus.SUGGESTED:
            db.delete(row)
    db.flush()

    created: list[Highlight] = []
    for score, spec in keep:
        highlight = Highlight(
            entry_id=entry.id,
            clinic_id=entry.clinic_id,
            patient_id=entry.patient_id,
            span_start=spec["span_start"],
            span_end=spec["span_end"],
            span_text=spec["span_text"],
            source_version_number=entry.version_number,
            risk_reason=spec["risk_reason"],
            # Points at the exact character span, so clicking it lands on the
            # words rather than on the entry — the brief's "source of truth"
            # requirement, made literal.
            provenance_pointer=entry_pointer(entry.id, spec["span_start"], spec["span_end"]),
            status=HighlightStatus.SUGGESTED,
            score=score,
            score_breakdown=scoring.encode_breakdown(spec["breakdown"]),
            feature_tags=json.dumps(spec["tags"]),
            created_by="system",
            created_by_role=Role.SYSTEM,
        )
        db.add(highlight)
        created.append(highlight)

    # Human-decided highlights still need rescoring: recency moves, tasks close,
    # and (from Phase 4) learned weights shift underneath them.
    for row in existing:
        if row.status is HighlightStatus.SUGGESTED:
            continue
        tags = decode_tags(row.feature_tags)
        score, breakdown = scoring.score_span(
            db,
            clinic_id=entry.clinic_id,
            timestamp=entry.timestamp,
            risk_level=entry.risk_level,
            tags=tags,
            open_task_count=open_tasks,
            decay_state=entry.decay_state,
        )
        row.score = score
        row.score_breakdown = scoring.encode_breakdown(breakdown)

    db.flush()
    return created


def _compose_reason(reasons: list[str], entry: Entry) -> str:
    """One short line, most specific reason first, capped for the card.

    An AI-sourced highlight says so in its own reason text. The clinician
    reading the Glance View should not have to click through to discover that
    the thing being asserted came from a machine.
    """
    lead = "; ".join(reasons[:2])
    if EntryType(entry.type) in AI_SCRIBED_TYPES:
        lead = f"{lead} (from AI-scribed note)"
    return lead[:300]


def refresh_patient_highlights(db: Session, patient_id: str, clinic_id: str) -> int:
    """Rescore every highlight for a patient. Used after task changes and by
    Phase 4 when learned weights move."""
    entries = (
        db.query(Entry)
        .filter(Entry.patient_id == patient_id, Entry.clinic_id == clinic_id)
        .all()
    )
    total = 0
    for entry in entries:
        total += len(refresh_entry_highlights(db, entry))
    return total


# --------------------------------------------------------------------------
# Manual highlighting
# --------------------------------------------------------------------------


def create_manual_highlight(
    db: Session,
    *,
    entry: Entry,
    span_start: int,
    span_end: int,
    created_by: str,
    created_by_role: Role,
    risk_reason: str | None = None,
) -> Highlight:
    """A clinician highlighting a phrase by hand.

    Recorded as ACCEPTED immediately — a human did not suggest it to themselves.
    It is also the strongest single signal Phase 4 has, which is why the caller
    pairs this with a `MANUAL_HIGHLIGHT` interaction row.
    """
    content = entry.content or ""
    span_start = max(0, min(span_start, len(content)))
    span_end = max(span_start, min(span_end, len(content)))
    span_text = content[span_start:span_end].strip()
    if not span_text:
        raise ValueError("highlight span is empty")

    span_tags, reasons = features.tag_span(span_text)
    tags = sorted(set(span_tags + features.entry_level_tags(entry.type, entry.risk_level)))
    score, breakdown = scoring.score_span(
        db,
        clinic_id=entry.clinic_id,
        timestamp=entry.timestamp,
        risk_level=entry.risk_level,
        tags=tags,
        open_task_count=_open_task_count(db, entry.id),
        decay_state=entry.decay_state,
    )
    # A clinician's own highlight outranks anything the machine proposed.
    score += 0.5
    breakdown["manual"] = 0.5

    highlight = Highlight(
        entry_id=entry.id,
        clinic_id=entry.clinic_id,
        patient_id=entry.patient_id,
        span_start=span_start,
        span_end=span_end,
        span_text=span_text,
        source_version_number=entry.version_number,
        risk_reason=risk_reason or _compose_reason(
            reasons or ["Marked by clinician"], entry
        ),
        provenance_pointer=entry_pointer(entry.id, span_start, span_end),
        status=HighlightStatus.ACCEPTED,
        score=round(score, 4),
        score_breakdown=scoring.encode_breakdown(breakdown),
        feature_tags=json.dumps(tags),
        created_by=created_by,
        created_by_role=created_by_role,
        decided_by=created_by,
        decided_at=_now(),
    )
    db.add(highlight)
    db.flush()
    return highlight


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def decode_tags(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def is_stale(highlight: Highlight, entry: Entry) -> bool:
    """True when the entry has been edited since this highlight was anchored."""
    return highlight.source_version_number != entry.version_number


def anchored_text(db: Session, highlight: Highlight, entry: Entry) -> str:
    """The words as they read *when the highlight was made*.

    On a stale highlight the current content may say something different at
    those offsets. Showing the current text under an old claim would be a
    quiet lie about what a clinician confirmed, so we resolve against the
    version snapshot the highlight was anchored to and let the UI say the entry
    has moved on.
    """
    if not is_stale(highlight, entry):
        return highlight.span_text
    version = (
        db.query(Version)
        .filter(
            Version.entry_id == entry.id,
            Version.version_number == highlight.source_version_number,
        )
        .first()
    )
    if version is None:
        return highlight.span_text
    snapshot = version.content_snapshot or ""
    return snapshot[highlight.span_start : highlight.span_end] or highlight.span_text
