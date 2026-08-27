"""Data decay — ageing the timeline without losing it.

The problem this solves is not disk. It is that a longitudinal record grows
monotonically while a consult stays ten minutes long, so without a policy the
signal-to-noise of the chart falls every year the patient stays alive.

The lifecycle is three states on `Entry.decay_state`:

| State  | Content in `Entry.content`      | Glance View                    |
|--------|---------------------------------|--------------------------------|
| `hot`  | full                            | full scoring                   |
| `warm` | full                            | scored at x0.7                 |
| `cold` | deterministic extractive summary| scored at x0.4, never excluded |

Four things make this safe enough to actually run, and each of them is a
constraint rather than a feature:

**Cold is reversible, always.** The full original is compressed into
`EntryArchive` before `Entry.content` is touched, and `restore()` puts it back
byte for byte. `test_data_decay.py` asserts the round trip. A lossy archival
step in a clinical record is a data-loss bug with a scheduler attached.

**Compression never calls an LLM.** The summary is extractive — real sentences
from the original, chosen by the same feature tagger the Glance View scores on.
An abstractive summariser that hallucinates during archival would corrupt the
record permanently and silently, at the exact moment nobody is looking. The
cost is that summaries read as clipped rather than fluent, which is the correct
trade for the only irreversible-looking operation in the system.

**Cold down-weights; it never hides.** `scoring.DECAY_MULTIPLIER` puts cold at
0.4, not zero. SCHEMA.md originally said cold entries were "excluded from
scoring" — building it revealed that as the wrong policy and it is corrected
there now (D-042). An entry can be the only record of an allergy and still be
four years old; age is a prior about relevance, never a proof of irrelevance.

**Some entries are never compressed at all.** Unresolved work, clinician-
confirmed highlights, flagged conflicts, and safety-critical clinical content
are held at `warm` however old they get. Old does not mean settled.

Scheduling: `run()` is invoked explicitly, by `POST /clinic/decay/run` or
`scripts/run_decay.py`. In production this is a nightly job; a prototype that
silently rewrote content on a timer would be much harder to reason about during
a demo, and the policy is the interesting part, not the cron entry.
"""

from __future__ import annotations

import base64
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.enums import CommentStatus, DecayState, HighlightStatus, RiskLevel, TaskStatus
from app.models import Comment, Entry, EntryArchive, Highlight, Task
from app.services import features

# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

WARM_AFTER_DAYS = 45
COLD_AFTER_DAYS = 180

# Below this the compression is not worth the indirection: the summary would be
# nearly the whole note and a reader gains nothing from a "restore" affordance.
MIN_LENGTH_TO_COMPRESS = 220

# Target length for the extractive summary.
SUMMARY_CHAR_BUDGET = 260

# Clinical content that is held at `warm` regardless of age. These are facts
# that do not expire: an allergy from 2019 kills exactly as effectively as one
# from last week.
PROTECTED_TAGS: frozenset[str] = frozenset(
    {
        "entity:allergy",
        "symptom:anaphylaxis",
        "symptom:suicidal",
        "symptom:self-harm",
        "symptom:sepsis",
    }
)

PROTECTED_RISK_LEVELS: frozenset[str] = frozenset(
    {str(RiskLevel.HIGH), str(RiskLevel.CRITICAL)}
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return _now()
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Archive round trip
# --------------------------------------------------------------------------


def _pack(text: str) -> tuple[str, int]:
    raw = text.encode("utf-8")
    return base64.b64encode(zlib.compress(raw, level=9)).decode("ascii"), len(raw)


def _unpack(packed: str) -> str:
    return zlib.decompress(base64.b64decode(packed.encode("ascii"))).decode("utf-8")


def archived_original(db: Session, entry: Entry) -> str | None:
    """The full pre-compression text of a cold entry, or None if not archived."""
    row = (
        db.query(EntryArchive)
        .filter(EntryArchive.entry_id == entry.id)
        .order_by(EntryArchive.archived_at.desc())
        .first()
    )
    if row is None:
        return None
    try:
        return _unpack(row.archived_content)
    except (zlib.error, ValueError):
        return None


def original_content(db: Session, entry: Entry) -> str:
    """The content a character offset should be interpreted against.

    Every stored span pointer (`entry://<id>#span:12-48`) was computed against
    the entry's full text. Compressing `Entry.content` would silently move
    every one of those offsets onto different words, which would break the
    brief's central promise that a highlight resolves to its source — so
    `provenance.resolve()` reads through this function rather than reading
    `Entry.content` directly. Cold storage changes what is cheap to read, not
    what is true.
    """
    if str(entry.decay_state) == str(DecayState.COLD):
        return archived_original(db, entry) or (entry.content or "")
    return entry.content or ""


# --------------------------------------------------------------------------
# Summarisation
# --------------------------------------------------------------------------


def summarise(text: str) -> str:
    """Extractive summary: the sentences that carry clinical tags, in order.

    Deterministic and offline by design (see module docstring). Sentences are
    kept verbatim and in their original sequence, so the summary is a subset of
    what was written rather than a paraphrase of it — nothing appears in a cold
    entry that a clinician did not actually write.
    """
    spans = features.sentences(text or "")
    if not spans:
        return (text or "")[:SUMMARY_CHAR_BUDGET]

    scored: list[tuple[int, int, str]] = []
    for index, (_, _, sentence) in enumerate(spans):
        tags, reasons = features.tag_span(sentence)
        weight = len(reasons) * 2 + len(tags)
        scored.append((weight, index, sentence))

    keep_indices: set[int] = set()
    budget = SUMMARY_CHAR_BUDGET
    for weight, index, sentence in sorted(scored, key=lambda item: (-item[0], item[1])):
        if weight == 0 and keep_indices:
            break
        if len(sentence) > budget:
            continue
        keep_indices.add(index)
        budget -= len(sentence) + 1
        if budget <= 0:
            break

    if not keep_indices:
        keep_indices = {0}

    return " ".join(spans[i][2] for i in sorted(keep_indices))


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    entry_id: str
    current_state: str
    target_state: str
    reason: str
    age_days: int
    protected: bool
    bytes_before: int = 0
    bytes_after: int = 0


def _protection_reason(db: Session, entry: Entry) -> str | None:
    """Why this entry may not be compressed, or None if it may be."""
    if str(entry.risk_level) in PROTECTED_RISK_LEVELS:
        return f"risk level is {entry.risk_level}"

    tags, _ = features.tag_span(original_content(db, entry))
    protected_tags = sorted(PROTECTED_TAGS.intersection(tags))
    if protected_tags:
        return f"carries safety-critical content ({', '.join(protected_tags)})"

    if entry.conflict_flagged or entry.supersedes_entry_id:
        return "part of a flagged clinician correction"

    open_tasks = (
        db.query(Task)
        .filter(Task.entry_id == entry.id)
        .filter(Task.status.in_([str(TaskStatus.OPEN), str(TaskStatus.IN_PROGRESS)]))
        .count()
    )
    if open_tasks:
        return "has unresolved tasks"

    open_comments = (
        db.query(Comment)
        .filter(Comment.entry_id == entry.id)
        .filter(Comment.status == CommentStatus.OPEN)
        .count()
    )
    if open_comments:
        return "has an open comment thread"

    accepted = (
        db.query(Highlight)
        .filter(Highlight.entry_id == entry.id)
        .filter(Highlight.status == HighlightStatus.ACCEPTED)
        .count()
    )
    if accepted:
        return "carries a clinician-confirmed highlight"

    return None


def classify(db: Session, entry: Entry, *, now: datetime | None = None) -> Verdict:
    """Decide what state this entry should be in. Pure — writes nothing."""
    now = now or _now()
    age_days = int((now - _aware(entry.timestamp)).total_seconds() // 86400)
    current = str(entry.decay_state)

    hold = _aware(entry.decay_hold_until) if entry.decay_hold_until else None
    if hold and hold > now:
        return Verdict(
            entry.id, current, current,
            "held after a manual restore", age_days, protected=True,
        )

    if age_days < WARM_AFTER_DAYS:
        return Verdict(
            entry.id, current, str(DecayState.HOT),
            f"{age_days}d old, under the {WARM_AFTER_DAYS}d warm threshold",
            age_days, protected=False,
        )

    protection = _protection_reason(db, entry)
    if protection is not None:
        return Verdict(
            entry.id, current, str(DecayState.WARM),
            f"held at warm: {protection}", age_days, protected=True,
        )

    if age_days < COLD_AFTER_DAYS:
        return Verdict(
            entry.id, current, str(DecayState.WARM),
            f"{age_days}d old, under the {COLD_AFTER_DAYS}d cold threshold",
            age_days, protected=False,
        )

    if len(entry.content or "") < MIN_LENGTH_TO_COMPRESS and current != str(DecayState.COLD):
        return Verdict(
            entry.id, current, str(DecayState.WARM),
            f"held at warm: too short to be worth compressing "
            f"({len(entry.content or '')} chars)",
            age_days, protected=False,
        )

    return Verdict(
        entry.id, current, str(DecayState.COLD),
        f"{age_days}d old and settled", age_days, protected=False,
    )


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------


def compress(db: Session, entry: Entry, *, now: datetime | None = None) -> Verdict:
    """Move one entry to cold: archive the original, replace content with a summary."""
    now = now or _now()
    original = original_content(db, entry)
    packed, original_length = _pack(original)

    existing = (
        db.query(EntryArchive).filter(EntryArchive.entry_id == entry.id).first()
    )
    if existing is None:
        db.add(
            EntryArchive(
                entry_id=entry.id,
                clinic_id=entry.clinic_id,
                archived_content=packed,
                compression="zlib+base64",
                original_length=original_length,
            )
        )
    else:
        existing.archived_content = packed
        existing.original_length = original_length
        existing.archived_at = now

    summary = summarise(original)
    entry.content = summary
    entry.decay_state = DecayState.COLD
    entry.decayed_at = now
    db.flush()
    return Verdict(
        entry.id, str(DecayState.COLD), str(DecayState.COLD),
        "compressed", 0, protected=False,
        bytes_before=original_length, bytes_after=len(summary.encode("utf-8")),
    )


def restore(
    db: Session, entry: Entry, *, hold_days: int = 30, now: datetime | None = None
) -> bool:
    """Rehydrate a cold entry to its full original text.

    A restore sets `decay_hold_until` so the next scheduled pass does not
    immediately undo it. Without the hold, a clinician who reopened a
    four-year-old note to read it properly would find it compressed again by
    morning, which reads as the system arguing with them.
    """
    now = now or _now()
    original = archived_original(db, entry)
    if original is None:
        return False
    entry.content = original
    entry.decay_state = DecayState.WARM
    entry.decayed_at = None
    entry.decay_hold_until = now + timedelta(days=hold_days)
    db.flush()
    return True


def run(
    db: Session,
    *,
    clinic_id: str | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> dict:
    """Evaluate every entry and apply (or preview) the transitions.

    `dry_run` defaults to True on purpose. This is the one operation in the
    system that rewrites stored clinical text, and the default for such a thing
    should be to describe what it would do.
    """
    now = now or _now()
    query = db.query(Entry)
    if clinic_id is not None:
        query = query.filter(Entry.clinic_id == clinic_id)

    changes: list[dict] = []
    unchanged = 0
    bytes_before = 0
    bytes_after = 0

    for entry in query.all():
        verdict = classify(db, entry, now=now)
        if verdict.target_state == verdict.current_state:
            unchanged += 1
            continue

        record = asdict(verdict)
        if verdict.target_state == str(DecayState.COLD):
            record["bytes_before"] = len((entry.content or "").encode("utf-8"))
            record["bytes_after"] = len(summarise(original_content(db, entry)).encode("utf-8"))
            if not dry_run:
                applied = compress(db, entry, now=now)
                record["bytes_before"] = applied.bytes_before
                record["bytes_after"] = applied.bytes_after
        elif not dry_run:
            if verdict.current_state == str(DecayState.COLD):
                # Moving out of cold means putting the real text back first.
                restore(db, entry, hold_days=0, now=now)
            entry.decay_state = DecayState(verdict.target_state)
            entry.decayed_at = now if verdict.target_state != str(DecayState.HOT) else None

        bytes_before += record.get("bytes_before", 0)
        bytes_after += record.get("bytes_after", 0)
        changes.append(record)

    if not dry_run:
        db.commit()

    return {
        "dry_run": dry_run,
        "evaluated": unchanged + len(changes),
        "unchanged": unchanged,
        "changed": len(changes),
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
        "bytes_saved": max(0, bytes_before - bytes_after),
        "policy": {
            "warm_after_days": WARM_AFTER_DAYS,
            "cold_after_days": COLD_AFTER_DAYS,
            "min_length_to_compress": MIN_LENGTH_TO_COMPRESS,
        },
        "changes": changes,
    }
