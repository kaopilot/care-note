"""The Glance View — everything a clinician needs in the first ten seconds.

Design constraints, in the order they mattered:

**Ten seconds is a hard budget, and it is a budget of attention, not just
milliseconds.** Every section here earns its place by answering a question a
clinician actually asks walking into a room: what changed since I last looked,
what could hurt this patient, what is outstanding, and how much of this came
from a machine. Anything that did not answer one of those was left in the
timeline where it belongs. Ranking is the easy half; refusing to surface things
is the half that makes the card readable.

**Scores are precomputed.** Highlights are generated and scored when entries are
written, so this endpoint reads rows and sorts them. Scoring the timeline on the
hot path would put an O(entries × sentences) loop inside a 300ms P95 budget for
no benefit — the inputs only change when someone writes.

**Nothing is filtered client-side.** The role-scoped type filter is applied in
SQL, the same way the timeline does it, so a patient's Glance View cannot
contain a clinician section that a stylesheet is hiding.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.enums import (
    AI_SCRIBED_TYPES,
    CommentStatus,
    EntryType,
    HighlightStatus,
    RiskLevel,
    Role,
    TaskStatus,
)
from app.models import (
    AIScribedNote,
    Comment,
    Entry,
    Highlight,
    Patient,
    PatientView,
    Task,
    User,
)
from app.core.timeutil import iso_utc
from app.security import policy
from app.services import highlights as highlight_service
from app.services import scoring

# A page refresh is not a new visit. Within this window the "since" marker holds
# still, so reading the what's-new group does not destroy it (D-033).
VIEW_SESSION_GAP = timedelta(minutes=20)

# ...but a marker that only rolls forward on a >20-minute gap never rolls
# forward at all for someone who keeps the chart open and refreshes through a
# shift. The window just widens, and "new since your last visit" quietly becomes
# "everything since this morning". This caps how stale the comparison point may
# get: past it, the marker advances on the next load even mid-session. Chosen as
# roughly one clinic session — long enough that a working session is not
# interrupted, short enough that the label stays true. See DECISIONS.md D-060.
MAX_MARKER_AGE = timedelta(hours=4)

# Below this, an AI summary is flagged as low confidence in the UI. Set where
# the offline summariser lands on a transcript full of hedging, so the flag has
# something real to fire on rather than being decorative.
LOW_CONFIDENCE_THRESHOLD = 0.6

MAX_HIGHLIGHTS = 6
MAX_WHATS_NEW = 8
MAX_RISK_FLAGS = 4

RISK_ORDER = {
    str(RiskLevel.CRITICAL): 4,
    str(RiskLevel.HIGH): 3,
    str(RiskLevel.MEDIUM): 2,
    str(RiskLevel.LOW): 1,
    str(RiskLevel.NONE): 0,
}

# Text labels paired with every risk level. The UI colours these too, but the
# label is what carries the meaning — a red chip with no words is unreadable to
# a colour-blind clinician and invisible to a screen reader.
RISK_LABEL = {
    str(RiskLevel.CRITICAL): "Critical",
    str(RiskLevel.HIGH): "High risk",
    str(RiskLevel.MEDIUM): "Medium risk",
    str(RiskLevel.LOW): "Low risk",
    str(RiskLevel.NONE): "No risk flag",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Last-viewed tracking
# --------------------------------------------------------------------------


def touch_view(db: Session, *, user_id: str, patient: Patient) -> datetime | None:
    """Record this view and return the timestamp to compare "what's new" against.

    Returns None the first time a user opens a patient — there is no "since"
    yet, and captioning an entire chart as new would be noise on the one view
    that most needs to be readable.
    """
    row = (
        db.query(PatientView)
        .filter(PatientView.user_id == user_id, PatientView.patient_id == patient.id)
        .first()
    )
    now = _now()

    if row is None:
        # `previous_viewed_at` is seeded to now rather than left NULL. Returning
        # None here is right — captioning a whole chart as new on the one view
        # that most needs to be readable is noise — but leaving the column NULL
        # meant the NEXT load in the same session also had nothing to compare
        # against, and so did the one after that. A clinician could open a
        # chart, write a note, reload, and still be told this was their first
        # look with nothing new. Seeding it makes the second load of a session
        # compare against the moment the session started. See D-060.
        db.add(
            PatientView(
                user_id=user_id,
                patient_id=patient.id,
                clinic_id=patient.clinic_id,
                last_viewed_at=now,
                previous_viewed_at=now,
                view_count=1,
            )
        )
        db.commit()
        return None

    last = _aware(row.last_viewed_at) or now
    since = _aware(row.previous_viewed_at)

    new_visit = now - last > VIEW_SESSION_GAP
    marker_stale = since is not None and now - since > MAX_MARKER_AGE

    if new_visit or marker_stale:
        # A genuinely new visit, or a session that has run long enough that the
        # old comparison point no longer means "last time you looked": the
        # previous view time becomes the marker.
        since = last
        row.previous_viewed_at = row.last_viewed_at
    row.last_viewed_at = now
    row.view_count += 1
    db.commit()
    return since


# --------------------------------------------------------------------------
# Clinical Glance View
# --------------------------------------------------------------------------


def build_glance(db: Session, *, role: Role, user_id: str, patient: Patient) -> dict:
    viewable = [str(t) for t in policy.viewable_types_for(role)]

    entries: list[Entry] = (
        db.query(Entry)
        .filter(Entry.patient_id == patient.id, Entry.clinic_id == patient.clinic_id)
        .filter(Entry.type.in_(viewable))
        .order_by(Entry.timestamp.desc())
        .all()
    )
    by_id = {entry.id: entry for entry in entries}

    since = touch_view(db, user_id=user_id, patient=patient)
    names = _user_names(db, patient.clinic_id)

    ai_notes = {
        note.entry_id: note
        for note in db.query(AIScribedNote)
        .filter(AIScribedNote.clinic_id == patient.clinic_id)
        .filter(AIScribedNote.entry_id.in_(list(by_id.keys()) or [""]))
        .all()
    }

    return {
        "patient": {
            "id": patient.id,
            "name": patient.name,
            "mrn": patient.mrn,
            "dob": patient.dob,
        },
        "generated_at": iso_utc(_now()),
        "since": iso_utc(since),
        "whats_new": _whats_new(entries, since, ai_notes),
        "highlights": _top_highlights(db, patient, by_id, ai_notes, role),
        "open_actions": _open_actions(db, patient, names),
        "risk_flags": _risk_flags(entries),
        "confidence_flags": _confidence_flags(entries, ai_notes),
        "conflicts": _conflicts(entries, by_id),
        "counts": _counts(db, patient, entries),
    }


def _user_names(db: Session, clinic_id: str) -> dict[str, str]:
    return {
        user.id: user.name
        for user in db.query(User).filter(User.clinic_id == clinic_id).all()
    }


def _entry_brief(entry: Entry, ai_notes: dict) -> dict:
    note = ai_notes.get(entry.id)
    first_line = (entry.content or "").strip().splitlines()
    return {
        "id": entry.id,
        "type": str(entry.type),
        "author_role": str(entry.author_role),
        "author_id": entry.author_id,
        "timestamp": iso_utc(entry.timestamp),
        "title": entry.title,
        "preview": (first_line[0][:180] if first_line else ""),
        "risk_level": str(entry.risk_level),
        "risk_label": RISK_LABEL.get(str(entry.risk_level), "No risk flag"),
        "is_ai_scribed": EntryType(entry.type) in AI_SCRIBED_TYPES,
        "confidence": note.confidence if note else None,
        "version_number": entry.version_number,
    }


def _whats_new(entries: list[Entry], since: datetime | None, ai_notes: dict) -> dict:
    """Entries written since this user last really looked.

    This is the brief's stated pain point — "no consolidated what changed" —
    answered without any AI: a timestamp comparison over data already loaded.
    """
    if since is None:
        return {"since": None, "count": 0, "entries": [], "first_visit": True}
    fresh = [entry for entry in entries if (_aware(entry.timestamp) or _now()) > since]
    return {
        "since": iso_utc(since),
        "count": len(fresh),
        "entries": [_entry_brief(entry, ai_notes) for entry in fresh[:MAX_WHATS_NEW]],
        "first_visit": False,
    }


def _top_highlights(
    db: Session, patient: Patient, by_id: dict[str, Entry], ai_notes: dict, role: Role
) -> list[dict]:
    """Accepted highlights first, then suggestions, each by score.

    Ordering by status before score is a trust decision, not a ranking
    nicety: something a clinician has already confirmed should not be pushed
    below a fresh machine guess just because the guess scored well this morning.
    """
    rows = (
        db.query(Highlight)
        .filter(Highlight.patient_id == patient.id)
        .filter(Highlight.clinic_id == patient.clinic_id)
        .filter(Highlight.status != HighlightStatus.REJECTED)
        .order_by(Highlight.score.desc())
        .all()
    )

    out: list[dict] = []
    for row in rows:
        entry = by_id.get(row.entry_id)
        if entry is None:
            continue  # entry type not visible to this role — never surface it
        note = ai_notes.get(entry.id)
        stale = highlight_service.is_stale(row, entry)
        out.append(
            {
                "id": row.id,
                "entry_id": row.entry_id,
                "span_start": row.span_start,
                "span_end": row.span_end,
                "span_text": highlight_service.anchored_text(db, row, entry),
                "risk_reason": row.risk_reason,
                "provenance_pointer": row.provenance_pointer,
                "status": str(row.status),
                "score": round(row.score, 3),
                "score_breakdown": scoring.decode_breakdown(row.score_breakdown),
                "feature_tags": highlight_service.decode_tags(row.feature_tags),
                "created_by_role": str(row.created_by_role),
                "is_manual": row.created_by_role != Role.SYSTEM,
                "stale": stale,
                "source_version_number": row.source_version_number,
                "entry_type": str(entry.type),
                "entry_title": entry.title,
                "entry_timestamp": iso_utc(entry.timestamp),
                "entry_author_role": str(entry.author_role),
                "is_ai_scribed": EntryType(entry.type) in AI_SCRIBED_TYPES,
                "ai_confidence": note.confidence if note else None,
                "can_decide": policy.can_decide_highlights(role),
            }
        )

    accepted = [h for h in out if h["status"] == str(HighlightStatus.ACCEPTED)]
    suggested = [h for h in out if h["status"] == str(HighlightStatus.SUGGESTED)]
    return (accepted + suggested)[:MAX_HIGHLIGHTS]


def _open_actions(db: Session, patient: Patient, names: dict[str, str]) -> list[dict]:
    """Open tasks and unresolved comment threads, newest first.

    Both appear because they are the same thing to a clinician — someone is
    waiting on something — even though the schema models them separately.
    """
    tasks = (
        db.query(Task)
        .filter(Task.patient_id == patient.id, Task.clinic_id == patient.clinic_id)
        .filter(Task.status.in_([str(TaskStatus.OPEN), str(TaskStatus.IN_PROGRESS)]))
        .order_by(Task.created_at.desc())
        .all()
    )
    actions = [
        {
            "kind": "task",
            "id": task.id,
            "description": task.description,
            "status": str(task.status),
            "assigned_to": task.assigned_to,
            "assigned_to_name": names.get(task.assigned_to or "", "Unassigned"),
            "assigned_to_role": str(task.assigned_to_role) if task.assigned_to_role else None,
            "entry_id": task.entry_id,
            "due_at": iso_utc(task.due_at),
            "created_at": iso_utc(task.created_at),
        }
        for task in tasks
    ]

    open_comments = (
        db.query(Comment)
        .join(Entry, Comment.entry_id == Entry.id)
        .filter(Entry.patient_id == patient.id)
        .filter(Comment.clinic_id == patient.clinic_id)
        .filter(Comment.status == CommentStatus.OPEN)
        .filter(Comment.parent_comment_id.is_(None))
        .order_by(Comment.created_at.desc())
        .limit(5)
        .all()
    )
    for comment in open_comments:
        mentions = _decode_list(comment.mentions)
        actions.append(
            {
                "kind": "comment",
                "id": comment.id,
                "description": comment.body[:160],
                "status": "open",
                "assigned_to": mentions[0] if mentions else None,
                "assigned_to_name": (
                    names.get(mentions[0], "the team") if mentions else "the team"
                ),
                "assigned_to_role": None,
                "entry_id": comment.entry_id,
                "due_at": None,
                "created_at": iso_utc(comment.created_at),
                "author_name": names.get(comment.author_id, "Unknown"),
                "author_role": str(comment.author_role),
            }
        )
    return actions


def _risk_flags(entries: list[Entry]) -> list[dict]:
    flagged = [
        entry
        for entry in entries
        if RISK_ORDER.get(str(entry.risk_level), 0) >= RISK_ORDER[str(RiskLevel.MEDIUM)]
    ]
    flagged.sort(
        key=lambda entry: (
            RISK_ORDER.get(str(entry.risk_level), 0),
            _aware(entry.timestamp) or _now(),
        ),
        reverse=True,
    )
    return [
        {
            "entry_id": entry.id,
            "level": str(entry.risk_level),
            "label": RISK_LABEL.get(str(entry.risk_level), "Risk"),
            "entry_type": str(entry.type),
            "title": entry.title,
            "timestamp": iso_utc(entry.timestamp),
            "is_ai_scribed": EntryType(entry.type) in AI_SCRIBED_TYPES,
        }
        for entry in flagged[:MAX_RISK_FLAGS]
    ]


def _confidence_flags(entries: list[Entry], ai_notes: dict) -> list[dict]:
    """AI summaries the system is not sure about.

    Kept separate from `risk_flags` on purpose. "This might be dangerous" and
    "this might be wrong" are different warnings and a clinician responds to
    them differently — collapsing them into one amber chip would lose that.
    """
    out = []
    for entry in entries:
        note = ai_notes.get(entry.id)
        if note is None or note.confidence is None:
            continue
        if note.confidence >= LOW_CONFIDENCE_THRESHOLD:
            continue
        out.append(
            {
                "entry_id": entry.id,
                "type": str(entry.type),
                "title": entry.title,
                "confidence": round(note.confidence, 2),
                "label": "Low AI confidence — verify against source",
                "session_id": note.session_id,
                "model_used": note.model_used,
                "timestamp": iso_utc(entry.timestamp),
            }
        )
    return out


def _conflicts(entries: list[Entry], by_id: dict[str, Entry]) -> list[dict]:
    """Entries where a clinician overrode earlier AI or patient content.

    D-007 resolves these in the clinician's favour immediately *and* keeps the
    disagreement visible. This is the surface that makes the second half true.
    """
    out = []
    for entry in entries:
        if not entry.conflict_flagged and not entry.supersedes_entry_id:
            continue
        superseded = by_id.get(entry.supersedes_entry_id or "")
        out.append(
            {
                "entry_id": entry.id,
                "entry_type": str(entry.type),
                "flagged": bool(entry.conflict_flagged),
                "supersedes_entry_id": entry.supersedes_entry_id,
                "supersedes_type": str(superseded.type) if superseded else None,
                "supersedes_title": superseded.title if superseded else None,
                "timestamp": iso_utc(entry.timestamp),
            }
        )
    return out


def _counts(db: Session, patient: Patient, entries: list[Entry]) -> dict:
    return {
        "entries": len(entries),
        "ai_scribed": sum(
            1 for entry in entries if EntryType(entry.type) in AI_SCRIBED_TYPES
        ),
        "open_tasks": (
            db.query(Task)
            .filter(Task.patient_id == patient.id, Task.clinic_id == patient.clinic_id)
            .filter(Task.status.in_([str(TaskStatus.OPEN), str(TaskStatus.IN_PROGRESS)]))
            .count()
        ),
    }


def _decode_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


# --------------------------------------------------------------------------
# Patient view
# --------------------------------------------------------------------------

# Plain-language labels. Written to be read by someone who is anxious, on a
# phone, and not a clinician. No abbreviations, no system vocabulary: a patient
# has "next steps", not "pending clinician actions".
PATIENT_SECTION_LABELS = {
    "next_steps": "What to do next",
    "updates": "What your care team wrote for you",
    "your_notes": "What you told us",
}


def build_patient_glance(db: Session, *, user_id: str, patient: Patient) -> dict:
    viewable = [str(t) for t in policy.viewable_types_for(Role.PATIENT)]
    entries = (
        db.query(Entry)
        .filter(Entry.patient_id == patient.id, Entry.clinic_id == patient.clinic_id)
        .filter(Entry.type.in_(viewable))
        .order_by(Entry.timestamp.desc())
        .all()
    )
    since = touch_view(db, user_id=user_id, patient=patient)

    instructions = [e for e in entries if EntryType(e.type) is EntryType.PATIENT_INSTRUCTION]
    summaries = [e for e in entries if EntryType(e.type) is EntryType.PATIENT_SUMMARY]
    own_notes = [e for e in entries if EntryType(e.type) is EntryType.PATIENT_NOTE]

    next_steps: list[dict] = []
    for entry in instructions[:2]:
        for line in _bullet_lines(entry.content):
            next_steps.append({"text": line, "entry_id": entry.id, "written_at": iso_utc(entry.timestamp)})

    return {
        "patient": {"id": patient.id, "name": patient.name},
        "generated_at": iso_utc(_now()),
        "since": iso_utc(since),
        "new_since_last_visit": (
            0 if since is None
            else sum(1 for e in entries if (_aware(e.timestamp) or _now()) > since)
        ),
        "labels": PATIENT_SECTION_LABELS,
        "next_steps": next_steps[:6],
        "updates": [
            {
                "id": entry.id,
                "title": entry.title,
                "content": entry.content,
                "written_at": iso_utc(entry.timestamp),
            }
            for entry in (summaries + instructions)[:3]
        ],
        "your_notes": [
            {
                "id": entry.id,
                "title": entry.title,
                "content": entry.content,
                "written_at": iso_utc(entry.timestamp),
            }
            for entry in own_notes[:3]
        ],
    }


def _bullet_lines(content: str | None) -> list[str]:
    """Split an instruction note into one action per line.

    Sentence-per-step, because "take this, bring that, book the other" as a
    single paragraph is where patient instructions get half-read.
    """
    text = (content or "").strip()
    if not text:
        return []
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip().lstrip("-•").strip()
        if not stripped:
            continue
        if len(stripped) > 140:
            parts = [p.strip() for p in stripped.replace(". ", ".\n").splitlines()]
            lines.extend(p for p in parts if len(p) > 4)
        else:
            lines.append(stripped)
    return lines
