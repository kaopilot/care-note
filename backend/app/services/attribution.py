"""Linking a generated summary back to the words that were actually spoken.

The Entry's own `provenance_pointer` names the session the note came from. That
is enough to answer "where did this note come from" and not enough to answer the
question a clinician actually asks, which is "where did *that line* come from".
This module answers the second one: for each line of a summary, which transcript
segment produced it, at what timestamp, from which speaker, with what recogniser
confidence.

Why matching, rather than asking the summariser to cite itself
--------------------------------------------------------------
The obvious design is to have the model emit citations alongside each bullet.
It is also the design that produces confident, checkable-looking pointers to
segments that do not support the line — models hallucinate citations at least as
readily as they hallucinate content, and a false citation is worse than none
because it survives review. A wrong pointer that *looks* checkable is the exact
failure this product exists to prevent.

So attribution is established here, after the fact, by comparing the summary
against the stored segments:

* **verbatim** — the segment's words appear in the line character for character
  (whitespace-normalised). The offline extractive summariser selects real
  utterances, so this is the common case, and the pointer is *proved* rather
  than claimed. Nobody has to trust the summariser for this to hold.
* **derived** — the line and the segment share enough distinctive vocabulary to
  be confident despite rewording. This is what a live model's output looks like.
  Scored, and labelled differently in the UI, because it is weaker evidence.
* **nothing** — no row is written. A line the model composed from several places,
  or invented, gets no source, and the UI says "no traceable source" rather than
  pointing somewhere plausible. See DECISIONS.md D-048.

The asymmetry is deliberate: the system's confidence in a citation should come
from evidence it can re-derive, not from an assurance it was given.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.core.enums import AttributionMatch
from app.core.provenance import transcript_pointer
from app.models import Entry, SummaryAttribution, TranscriptSegment

# Words carried by almost every clinical utterance. Left in for the verbatim
# comparison (which needs the exact string) and stripped for the derived one,
# where "the patient said the" is not evidence of anything.
_STOPWORDS = frozenset(
    """a an and are as at be been but by can cannot could did do does for from
    had has have he her him his how i if in is it its me my no not of on or our
    out she should so some that the their them then there these they this to too
    up us was we were what when which who will with would you your yes okay ok
    just still now got very much really quite bit like about""".split()
)

_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_BULLET_RE = re.compile(r"^\s*-\s+")

# A line must share this proportion of its distinctive words with a segment, and
# at least this many of them, before a `derived` link is recorded. Set by hand
# against the fixtures: lower and unrelated bullets in the same consult start
# matching each other, which would be worse than no attribution at all.
_DERIVED_RATIO = 0.55
_DERIVED_MIN_TOKENS = 3


def _normalise(text: str) -> str:
    """Collapse whitespace and case for comparison, without altering storage."""
    return " ".join(text.lower().split())


def _content_tokens(text: str) -> set[str]:
    return {word for word in _WORD_RE.findall(text.lower()) if word not in _STOPWORDS}


def _summary_lines(content: str) -> list[tuple[int, int, str]]:
    """Every attributable line of a rendered summary, with its character span.

    Section headers ("Key points") and blank lines are skipped — they are
    scaffolding this codebase wrote, not anything anyone said. The headline is
    skipped for the same reason: it is chosen from a fixed table in
    `services/scribe.py`, so attributing it to a segment would be inventing a
    source for a string we hardcoded.
    """
    rows: list[tuple[int, int, str]] = []
    offset = 0
    for line in content.split("\n"):
        stripped = line.strip()
        bullet = _BULLET_RE.match(line)
        if stripped and bullet:
            start = offset + bullet.end()
            end = offset + len(line.rstrip())
            if end > start:
                rows.append((start, end, content[start:end]))
        offset += len(line) + 1  # +1 for the newline consumed by split
    return rows


def _best_segment(
    line: str, segments: list[TranscriptSegment]
) -> tuple[TranscriptSegment, AttributionMatch, float] | None:
    """The segment a line came from, or None if that cannot be established."""
    normalised_line = _normalise(line)
    if not normalised_line:
        return None

    # Verbatim first, and prefer the longest containing segment so a short
    # interjection ("Yes doctor.") cannot win over the utterance that actually
    # carries the content.
    verbatim: list[TranscriptSegment] = []
    for segment in segments:
        normalised_segment = _normalise(segment.redacted_text)
        if not normalised_segment:
            continue
        if normalised_segment == normalised_line or normalised_segment in normalised_line:
            verbatim.append(segment)
    if verbatim:
        best = max(verbatim, key=lambda s: len(_normalise(s.redacted_text)))
        return best, AttributionMatch.VERBATIM, 1.0

    line_tokens = _content_tokens(line)
    if len(line_tokens) < _DERIVED_MIN_TOKENS:
        return None

    best_segment: TranscriptSegment | None = None
    best_ratio = 0.0
    for segment in segments:
        shared = line_tokens & _content_tokens(segment.redacted_text)
        if len(shared) < _DERIVED_MIN_TOKENS:
            continue
        ratio = len(shared) / len(line_tokens)
        if ratio > best_ratio:
            best_segment, best_ratio = segment, ratio

    if best_segment is not None and best_ratio >= _DERIVED_RATIO:
        return best_segment, AttributionMatch.DERIVED, round(best_ratio, 2)
    return None


def link_summary_to_segments(
    db: Session, *, entry: Entry, session_id: str
) -> list[SummaryAttribution]:
    """Write attribution rows for one AI-scribed entry. Idempotent per version.

    Called after the scribe pipeline has stored both the entry and its segments.
    Does not commit — the caller owns the transaction, so a capture is one
    atomic write rather than a note that exists without its provenance.
    """
    segments = (
        db.query(TranscriptSegment)
        .filter(
            TranscriptSegment.session_id == session_id,
            TranscriptSegment.clinic_id == entry.clinic_id,
        )
        .order_by(TranscriptSegment.sequence)
        .all()
    )
    if not segments:
        return []

    # Rebuilt rather than appended to, so re-running against a new version does
    # not leave rows pointing at offsets from the previous one.
    db.query(SummaryAttribution).filter(
        SummaryAttribution.entry_id == entry.id,
        SummaryAttribution.source_version_number == entry.version_number,
    ).delete(synchronize_session=False)

    created: list[SummaryAttribution] = []
    for start, end, line in _summary_lines(entry.content):
        match = _best_segment(line, segments)
        if match is None:
            continue
        segment, match_type, score = match
        row = SummaryAttribution(
            entry_id=entry.id,
            clinic_id=entry.clinic_id,
            session_id=session_id,
            span_start=start,
            span_end=end,
            source_version_number=entry.version_number,
            segment_sequence=segment.sequence,
            provenance_pointer=transcript_pointer(session_id, segment.sequence),
            match_type=str(match_type),
            match_score=score,
        )
        db.add(row)
        created.append(row)

    db.flush()
    return created


def coverage(attributions: list[SummaryAttribution], entry: Entry) -> dict:
    """How much of a summary is traceable — reported, not hidden.

    A note where three of eight lines resolve to spoken words is a different
    object from one where all eight do, and the clinician reading it should be
    able to tell which they are holding.
    """
    total_lines = len(_summary_lines(entry.content))
    linked = len(attributions)
    return {
        "attributable_lines": total_lines,
        "linked_lines": linked,
        "verbatim": sum(
            1 for a in attributions if a.match_type == str(AttributionMatch.VERBATIM)
        ),
        "derived": sum(
            1 for a in attributions if a.match_type == str(AttributionMatch.DERIVED)
        ),
        "coverage": round(linked / total_lines, 2) if total_lines else 0.0,
    }
