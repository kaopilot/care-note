"""Patients and their timeline — the first real routes on the Phase 0 pattern.

Phase 1's job is to prove the architecture end to end on the thinnest possible
real slice. So this module is deliberately small: list patients, read one
patient, read a patient's timeline, write one entry to it. No comments, no
highlights, no AI. Those are Phase 2, and they will hang off exactly these
shapes.

What this module is actually testing, structurally:

* Not one handler below mentions `clinic_id` in a filter. Every read goes
  through `scope.query()` / `scope.get_or_404()`, which apply the clinic
  predicate themselves. If the fusion in `rbac.py` were decorative, a
  cross-clinic read would succeed here and `tests/test_phase1_cross_clinic.py`
  would fail.
* Type-level filtering is `policy.viewable_types_for(role)` applied as a SQL
  `IN` clause, not a post-fetch list comprehension. A row a role may not see is
  never loaded into memory, so it cannot leak through a count, a timestamp, or
  a debug repr.
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.audit_logging import log_event
from app.core.enums import AI_SCRIBED_TYPES, EntryType, InteractionAction, RiskLevel, Role
from app.core.provenance import entry_pointer
from app.core.sanitization import ContentTooLongError, prepare_content
from app.models import AIScribedNote, AuditLog, Comment, Entry, Highlight, Patient, User, Version
from app.routes.schemas import EntryOut, entry_out
from app.security import policy
from app.security.rbac import AccessScope, require_access
from app.services import features, highlights
from app.services.interactions import record_interaction

router = APIRouter(tags=["patients"])


# --------------------------------------------------------------------------
# Wire formats
# --------------------------------------------------------------------------


class PatientOut(BaseModel):
    id: str
    name: str
    dob: str | None = None
    mrn: str | None = None
    clinic_id: str


class EntryCreate(BaseModel):
    type: EntryType
    content: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=300)
    risk_level: RiskLevel = RiskLevel.NONE


def _to_out(entry: Entry) -> EntryOut:
    """Minimal serialisation, for the single-entry routes.

    The timeline route uses `enrich_entries` instead, which batches the counts
    and joins rather than issuing them per row.
    """
    return entry_out(entry)


def enrich_entries(scope: AccessScope, entries: list[Entry]) -> list[EntryOut]:
    """Serialise a timeline with its metadata, in a fixed number of queries.

    Author names, AI provenance, comment counts and highlight counts are all
    things the timeline must show. Fetching them per entry is the obvious
    implementation and turns a 20-entry chart into 80 round trips; four grouped
    queries keep the shape flat regardless of chart size.
    """
    if not entries:
        return []

    entry_ids = [entry.id for entry in entries]
    names = {
        user.id: user.name
        for user in scope.db.query(User).filter(User.clinic_id == scope.clinic_id).all()
    }
    ai_notes = {
        note.entry_id: note
        for note in scope.query(AIScribedNote)
        .filter(AIScribedNote.entry_id.in_(entry_ids))
        .all()
    }

    comment_totals: dict[str, int] = {}
    open_comments: dict[str, int] = {}
    for comment in scope.query(Comment).filter(Comment.entry_id.in_(entry_ids)).all():
        # A patient must not learn that internal discussion exists, let alone
        # how much of it. Counting it for them would leak the fact of the
        # conversation even though the bodies stay hidden.
        if comment.is_internal and not policy.can_view_internal_comments(scope.role):
            continue
        comment_totals[comment.entry_id] = comment_totals.get(comment.entry_id, 0) + 1
        if str(comment.status) == "open":
            open_comments[comment.entry_id] = open_comments.get(comment.entry_id, 0) + 1

    highlight_totals: dict[str, int] = {}
    if policy.can_view_internal_comments(scope.role):
        for highlight in (
            scope.query(Highlight).filter(Highlight.entry_id.in_(entry_ids)).all()
        ):
            if str(highlight.status) == "rejected":
                continue
            highlight_totals[highlight.entry_id] = (
                highlight_totals.get(highlight.entry_id, 0) + 1
            )

    return [
        entry_out(
            entry,
            author_name=(
                "Care Note AI" if str(entry.author_role) == "system"
                else names.get(entry.author_id)
            ),
            ai_note=ai_notes.get(entry.id),
            comment_count=comment_totals.get(entry.id, 0),
            open_comment_count=open_comments.get(entry.id, 0),
            highlight_count=highlight_totals.get(entry.id, 0),
            # Drives whether the client offers an edit affordance. The server
            # refuses the write regardless of what the client decides to draw.
            editable_by_me=policy.can_write_type(scope.role, EntryType(entry.type)),
        )
        for entry in entries
    ]


# --------------------------------------------------------------------------
# Patients
# --------------------------------------------------------------------------


@router.get("/patients", response_model=list[PatientOut])
def list_patients(scope: AccessScope = Depends(require_access())) -> list[PatientOut]:
    """Patients this caller may see.

    Staff/clinician/admin get everyone in their clinic. A patient login gets
    exactly one row: itself. Note there is no `if role == patient` branch doing
    a *second* query — the same clinic-scoped query is narrowed further.
    """
    query = scope.query(Patient)
    if scope.role is Role.PATIENT:
        query = query.filter(Patient.id == scope.patient_id)
    return [
        PatientOut(id=p.id, name=p.name, dob=p.dob, mrn=p.mrn, clinic_id=p.clinic_id)
        for p in query.order_by(Patient.name).all()
    ]


@router.get("/patients/{patient_id}", response_model=PatientOut)
def get_patient(
    patient_id: str, scope: AccessScope = Depends(require_access())
) -> PatientOut:
    """Read one patient.

    Two different refusals, deliberately:
      * cross-clinic  -> 404 from `get_or_404`, so the response cannot be used
        to probe whether an id exists in another clinic;
      * same clinic, wrong person, patient login -> 403, because the id does
        exist and pretending otherwise would be a lie to a legitimate user.
    """
    scope.assert_patient_visible(patient_id)
    patient = scope.get_or_404(Patient, patient_id)
    return PatientOut(
        id=patient.id,
        name=patient.name,
        dob=patient.dob,
        mrn=patient.mrn,
        clinic_id=patient.clinic_id,
    )


# --------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------


@router.get("/patients/{patient_id}/entries", response_model=list[EntryOut])
def list_entries(
    patient_id: str, scope: AccessScope = Depends(require_access())
) -> list[EntryOut]:
    """The timeline, newest first, filtered to what this role may read.

    The type filter is pushed into SQL rather than applied after fetching.
    Filtering in Python would mean rows the caller may not see are briefly in
    process memory, where a stray log line, an exception repr, or a later
    refactor that returns `len(rows)` can leak them. Never loading them is a
    stronger property than loading and discarding them.
    """
    scope.assert_patient_visible(patient_id)
    scope.get_or_404(Patient, patient_id)  # 404s a cross-clinic patient_id

    viewable = [str(t) for t in policy.viewable_types_for(scope.role)]
    entries = (
        scope.query(Entry)
        .filter(Entry.patient_id == patient_id)
        .filter(Entry.type.in_(viewable))
        .order_by(Entry.timestamp.desc())
        .all()
    )

    log_event(
        actor_id=scope.user_id,
        action="entry.list",
        target_type="patient",
        target_id=patient_id,
        clinic_id=scope.clinic_id,
        metadata={"role": str(scope.role), "returned": len(entries)},
    )
    return enrich_entries(scope, entries)


@router.get("/entries/{entry_id}", response_model=EntryOut)
def get_entry(entry_id: str, scope: AccessScope = Depends(require_access())) -> EntryOut:
    """Read one entry by id — the direct-API path an attacker would actually use.

    A UI that hides clinician sections from staff is worth nothing if this route
    hands one over when asked by id. Both dimensions apply: `get_or_404` refuses
    another clinic's entry, and `assert_can_view_type` refuses a type this role
    may not read.
    """
    entry = scope.get_or_404(Entry, entry_id)
    scope.assert_patient_visible(entry.patient_id)
    scope.assert_can_view_type(entry.type)
    return _to_out(entry)


@router.post(
    "/patients/{patient_id}/entries",
    response_model=EntryOut,
    status_code=status.HTTP_201_CREATED,
)
def create_entry(
    patient_id: str,
    payload: EntryCreate,
    scope: AccessScope = Depends(require_access()),
) -> EntryOut:
    """Append a manual entry to the timeline.

    Authorship is taken from the token, never from the request body: there is no
    `author_id` field in `EntryCreate` to spoof. The write matrix in policy.py
    is what makes "staff cannot overwrite clinician notes" and its converse true
    by construction rather than by a check someone might forget.

    AI-scribed types are refused here outright. They carry `author_role=system`
    and must originate from the Phase 2 scribe pipeline, which routes through
    the redaction chokepoint. Allowing a human caller to POST one would let a
    client fabricate machine provenance.
    """
    scope.assert_patient_visible(patient_id)
    patient = scope.get_or_404(Patient, patient_id)

    if payload.type in AI_SCRIBED_TYPES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "AI-scribed entries cannot be created through this route; they are "
                "emitted by the scribe pipeline with author_role=system"
            ),
        )
    scope.assert_can_write_type(payload.type)

    # The one write-path chokepoint (shared context / D-015). Returns the text
    # to store VERBATIM plus markers describing what it looked like. Markers are
    # recorded as metadata; the content is never rewritten, because silently
    # altering "dose <5mg" is a worse bug than the injection it would prevent.
    try:
        content, markers = prepare_content(payload.content)
    except ContentTooLongError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    title, title_markers = prepare_content(payload.title) if payload.title else ("", [])

    entry = Entry(
        patient_id=patient.id,
        clinic_id=scope.clinic_id,  # from the token, not the body
        author_role=scope.role,
        author_id=scope.user_id,
        type=payload.type,
        title=title or None,
        content=content,
        risk_level=payload.risk_level,
        version_number=1,
    )
    scope.db.add(entry)
    scope.db.flush()  # need the id to build its own provenance pointer

    # A manually authored entry is its own provenance: it was written here, not
    # derived from a transcript or an AI session. Making that explicit keeps the
    # "every entry has a provenance_pointer" invariant true for manual notes
    # too, so Phase 2 never has to special-case a null.
    entry.provenance_pointer = entry_pointer(entry.id)

    version = Version(
        entry_id=entry.id,
        version_number=1,
        content_snapshot=content,
        title_snapshot=title or None,
        risk_level_snapshot=str(payload.risk_level),
        edited_by=scope.user_id,
        edited_by_role=scope.role,
        change_summary="created",
    )
    scope.db.add(version)
    scope.db.flush()
    entry.current_version_id = version.id

    scope.db.add(
        AuditLog(
            actor_id=scope.user_id,
            actor_role=scope.role,
            clinic_id=scope.clinic_id,
            action="entry.create",
            target_type="entry",
            target_id=entry.id,
            # Metadata only. Length and markers, never the note itself.
            audit_metadata=json.dumps(
                {
                    "type": str(payload.type),
                    "version": 1,
                    "content_length": len(content),
                    "injection_markers": markers + title_markers,
                }
            ),
        )
    )
    scope.db.commit()
    scope.db.refresh(entry)

    # Score the new entry now, not when the Glance View is next opened. The
    # Glance View has a 300ms P95 budget and this is the work that would
    # otherwise land inside it.
    highlights.refresh_entry_highlights(scope.db, entry)
    # CREATE, not EDIT. Phase 4 found that logging authorship as an engagement
    # signal would have taught the ranking "whatever this clinic writes about
    # most", which is volume rather than attention — so this row is recorded for
    # the behavioural history and carries weight 0.0 in learning (D-039).
    record_interaction(
        scope.db,
        user_id=scope.user_id,
        user_role=scope.role,
        clinic_id=scope.clinic_id,
        action=InteractionAction.CREATE,
        target_type="entry",
        target_id=entry.id,
        tags=features.entry_level_tags(entry.type, entry.risk_level)
        + features.tag_span(content)[0],
    )
    scope.db.commit()

    log_event(
        actor_id=scope.user_id,
        action="entry.create",
        target_type="entry",
        target_id=entry.id,
        clinic_id=scope.clinic_id,
        metadata={"type": str(payload.type), "markers": len(markers + title_markers)},
    )
    return _to_out(entry)
