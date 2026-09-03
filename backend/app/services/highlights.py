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

from app.core.enums import AI_SCRIBED_TYPES, DecayState, EntryType, HighlightStatus, Role
from app.core.provenance import entry_pointer
from app.models import Entry, Highlight, Task, Version
from app.services import features, scoring

# At most this many machine suggestions per entry. The Glance View is a
# 10-second surface; an entry that generates nine candidates has produced noise,
# not signal, and the top few carry almost all the value.
MAX_SUGGESTIONS_PER_ENTRY = 3

# Below this, a candidate is not worth a clinician's attention at all.
MIN_SUGGESTION_SCORE = 0.12

# A clinician's own mark outranks anything the machine proposed. Held here
# rather than inline in `create_manual_highlight` because RESCORING has to
# re-apply it: the bonus is a property of who made the highlight, not a
# one-off adjustment at creation time. Applying it in only one of the two
# places is what made hand-marked spans silently fall off the Glance View.
MANUAL_HIGHLIGHT_BONUS = 0.5

# --------------------------------------------------------------------------
# Exposure bias
# --------------------------------------------------------------------------
#
# The learning loop only ever sees feedback on spans it chose to surface. A tag
# that scores just below the cut is never shown, so it is never accepted, so it
# never gains weight, so it is never shown — the ranking converges on whatever
# it happened to favour early and cannot discover that it was wrong. This is a
# structural property of learning from your own output, not a tuning problem.
#
# The mitigation is deliberately small: **one of the slots per entry is reserved
# for a candidate carrying a tag the clinic has never given feedback on**, when
# such a candidate exists and clears the minimum score. It is not randomised —
# an epsilon-greedy coin flip would make the Glance View non-deterministic
# between loads, which for a clinical surface is a worse property than the bias
# it fixes. Deterministic on (entry content, feedback history) means the same
# chart shows the same card, and the exploration slot resolves the moment the
# clinic gives feedback on that tag once.
#
# It is bounded by the same MIN_SUGGESTION_SCORE floor as everything else, so
# exploration can promote an under-explored candidate over a marginally better
# known one — it can never surface something the rules found clinically
# meaningless. See DECISIONS.md D-069.
EXPLORATION_SLOTS = 1


def _keep_with_exploration(
    candidates: list[tuple[float, dict]], existing: list[Highlight]
) -> list[tuple[float, dict]]:
    """Top-scoring candidates, with one slot held for an unexposed tag."""
    if len(candidates) <= MAX_SUGGESTIONS_PER_ENTRY:
        return candidates

    top = candidates[: MAX_SUGGESTIONS_PER_ENTRY]
    if EXPLORATION_SLOTS <= 0:
        return top

    # Tags that have already been surfaced on this entry — those have had their
    # chance at feedback, whatever came of it.
    exposed: set[str] = set()
    for row in existing:
        exposed.update(decode_tags(row.feature_tags))
    for _, spec in top:
        exposed.update(spec["tags"])

    for score, spec in candidates[MAX_SUGGESTIONS_PER_ENTRY:]:
        if not spec["tags"]:
            continue
        if any(tag in exposed for tag in spec["tags"]):
            continue
        # Displace the weakest of the top, not the strongest.
        return top[: MAX_SUGGESTIONS_PER_ENTRY - EXPLORATION_SLOTS] + [(score, spec)]
    return top


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

    # A cold entry holds a compressed summary in `content` (services/decay.py).
    # Generating new spans against it would mint provenance pointers whose
    # offsets index the summary while every existing pointer indexes the
    # original — two incompatible frames of reference in one table. Existing
    # highlights are still rescored below, so a cold entry keeps moving with
    # recency and learned weight; it just stops producing new claims.
    cold = str(entry.decay_state) == str(DecayState.COLD)

    candidates: list[tuple[float, dict]] = []
    for start, end, span_text in ([] if cold else features.sentences(entry.content or "")):
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
    keep = _keep_with_exploration(candidates, existing)

    # Suggestions are UPDATED IN PLACE where the same span comes back, not
    # deleted and re-created. A highlight's id is what the Glance View hands to
    # accept/reject, and this function runs on every write to the chart — entry
    # edits, task changes, comment resolution, another accept. Minting new ids
    # each pass meant the card a clinician was looking at held ids that no
    # longer existed, so Confirm returned "Highlight not found" for every
    # suggestion after the first. Keyed on (span_start, span_end): the same
    # words are the same claim. See DECISIONS.md D-059.
    #
    # `==`, not `is`. `Highlight.status` is declared Mapped[HighlightStatus] but
    # stored in a String column, so a row loaded from the database comes back as
    # a plain `str` and an identity check silently never matches. See D-055.
    suggested_by_span = {
        (row.span_start, row.span_end): row
        for row in existing
        if row.status == HighlightStatus.SUGGESTED
    }
    kept_spans: set[tuple[int, int]] = set()

    created: list[Highlight] = []
    for score, spec in keep:
        span = (spec["span_start"], spec["span_end"])
        kept_spans.add(span)
        pointer = entry_pointer(entry.id, spec["span_start"], spec["span_end"])

        row = suggested_by_span.get(span)
        if row is not None:
            # Same span, refreshed content. The id — and therefore any accept
            # or reject already in flight against it — survives.
            row.span_text = spec["span_text"]
            row.source_version_number = entry.version_number
            row.risk_reason = spec["risk_reason"]
            row.provenance_pointer = pointer
            row.score = score
            row.score_breakdown = scoring.encode_breakdown(spec["breakdown"])
            row.feature_tags = json.dumps(spec["tags"])
            created.append(row)
            continue

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
            provenance_pointer=pointer,
            status=HighlightStatus.SUGGESTED,
            score=score,
            score_breakdown=scoring.encode_breakdown(spec["breakdown"]),
            feature_tags=json.dumps(spec["tags"]),
            created_by="system",
            created_by_role=Role.SYSTEM,
        )
        db.add(highlight)
        created.append(highlight)

    # Suggestions this pass no longer produces are dropped. They are machine
    # output with no human investment in them, so removing them is free;
    # accepted and rejected rows are never touched here.
    for span, row in suggested_by_span.items():
        if span not in kept_spans:
            db.delete(row)

    # Human-decided highlights still need rescoring: recency moves, tasks close,
    # and (from Phase 4) learned weights shift underneath them.
    for row in existing:
        if row.status == HighlightStatus.SUGGESTED:  # `==`, not `is` — D-055
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
        # Re-apply the bonus a hand-marked span was created with. Without this
        # the very next refresh — which the manual-highlight route itself
        # triggers — recomputed the score from `score_span` alone and erased
        # it, dropping the clinician's own mark below the machine's suggestions
        # and, once six highlights were accepted, off the card entirely.
        if row.created_by_role != Role.SYSTEM:  # `!=`, not `is not` — D-055
            score = round(score + MANUAL_HIGHLIGHT_BONUS, 4)
            breakdown["manual"] = MANUAL_HIGHLIGHT_BONUS
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
    if str(entry.decay_state) == str(DecayState.COLD):
        # The clinician is looking at a summary, so their character offsets
        # index different text from every other pointer on this entry. Restore
        # first — one click, and it is a truthful refusal rather than a
        # highlight that silently points at the wrong words.
        raise ValueError(
            "this entry is archived; restore it to full text before highlighting"
        )

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
    # A clinician's own highlight outranks anything the machine proposed. The
    # same bonus is re-applied by `refresh_entry_highlights` on every later
    # rescore — see MANUAL_HIGHLIGHT_BONUS.
    score += MANUAL_HIGHLIGHT_BONUS
    breakdown["manual"] = MANUAL_HIGHLIGHT_BONUS

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
    """True when the live entry text is no longer what this highlight anchored to.

    Two ways that happens, and for a long time this function only knew about one.

    **An edit.** A new version, so `version_number` moves and the comparison
    below catches it. This is the case scenario 16 describes and the one the
    side-by-side UI was built for.

    **Decay compression.** `decay.compress` replaces `Entry.content` with an
    extractive summary and deliberately does *not* create a version — archival
    is not an authorship event and putting it in the clinical revision history
    would be worse. But `version_number` was the only thing staleness was
    reading, so a compressed entry reported `stale=False` while its content had
    been wholesale replaced: a highlight anchored to "mild ankle swelling"
    resolved to `'ing in the evenings'` at the same offsets, with no warning and
    no side-by-side, which is exactly the "silently point at different text"
    failure the mechanism exists to prevent.

    A cold entry's content is a summary, so it matches no version snapshot by
    construction — every highlight on it is stale, whatever its version number
    says. See DECISIONS.md D-102.
    """
    if str(entry.decay_state) == str(DecayState.COLD):
        return True
    return highlight.source_version_number != entry.version_number


def stale_reason(highlight: Highlight, entry: Entry) -> str | None:
    """Why this highlight is stale — `edited`, `archived`, or None.

    The two are stale for different reasons and the clinician needs different
    words for them. An edit moved the version number, so "v2 → v5" says what
    happened. Compression moves no version number, so the same sentence renders
    as "v1 → v1", which reads like a bug and tells nobody anything. See
    DECISIONS.md D-102.
    """
    if str(entry.decay_state) == str(DecayState.COLD):
        return "archived"
    if highlight.source_version_number != entry.version_number:
        return "edited"
    return None


def current_text(entry: Entry, highlight: Highlight) -> str | None:
    """Whatever now occupies the highlight's coordinates in the live entry.

    The counterpart to `anchored_text`. That function deliberately refuses to
    show current text under an old claim; this one exposes it explicitly, so
    the UI can put the two side by side and let a clinician see *what changed*
    rather than only being told that something did.

    Returns None when the offsets no longer land inside the content — an entry
    edited shorter is the common case. That is a real answer and the UI says
    "this part of the note no longer exists", which is more useful than a
    truncated fragment that reads like a quote (D-076).
    """
    content = entry.content or ""
    start, end = highlight.span_start, highlight.span_end
    if start is None or end is None:
        return None
    if str(entry.decay_state) == str(DecayState.COLD):
        # The offsets index the original, and `content` is now a summary built
        # from a different subset of it. Slicing anyway returns a fragment that
        # reads like a quote and is not one. None makes the UI say the span is
        # not in the shortened copy, and the full original can be restored.
        return None
    if start < 0 or end > len(content) or start >= end:
        return None
    return content[start:end]


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
