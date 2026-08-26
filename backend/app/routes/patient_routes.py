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
from app.core.enums import AI_SCRIBED_TYPES, EntryType, RiskLevel, Role
from app.core.provenance import entry_pointer
from app.core.sanitization import ContentTooLongError, prepare_content
from app.models import AuditLog, Entry, Patient, Version
from app.security import policy
from app.security.rbac import AccessScope, require_access

router = APIRouter(tags=["patients"])


# --------------------------------------------------------------------------
# Wire formats
# --------------------------------------------------------------------------


class PatientOut(BaseModel):
    id: str
    name: str
    dob: str
    mrn: str
    clinic_id: str


class EntryOut(BaseModel):
    """One timeline entry.

    Every field the shared context requires on a timeline entry is present and
    non-optional in the response: author_role, author_id, timestamp, type,
    provenance_pointer. `is_ai_scribed` is derived rather than stored so the
    client cannot disagree with the server about which notes are machine
    output — the visual distinction the brief requires is driven from here.
    """

    id: str
    patient_id: str
    author_role: str
    author_id: str
    timestamp: datetime
    type: str
    title: str | None
    content: str
    risk_level: str
    provenance_pointer: str | None
    version_number: int
    is_ai_scribed: bool


class EntryCreate(BaseModel):
    type: EntryType
    content: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=300)
    risk_level: RiskLevel = RiskLevel.NONE


def _to_out(entry: Entry) -> EntryOut:
    return EntryOut(
        id=entry.id,
        patient_id=entry.patient_id,
        author_role=str(entry.author_role),
        author_id=entry.author_id,
        timestamp=entry.timestamp,
        type=str(entry.type),
        title=entry.title,
        content=entry.content,
        risk_level=str(entry.risk_level),
        provenance_pointer=entry.provenance_pointer,
        version_number=entry.version_number,
        is_ai_scribed=EntryType(entry.type) in AI_SCRIBED_TYPES,
    )


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
    return [_to_out(e) for e in entries]


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

    log_event(
        actor_id=scope.user_id,
        action="entry.create",
        target_type="entry",
        target_id=entry.id,
        clinic_id=scope.clinic_id,
        metadata={"type": str(payload.type), "markers": len(markers + title_markers)},
    )
    return _to_out(entry)
