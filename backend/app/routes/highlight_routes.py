"""Highlights and provenance resolution.

The brief's hard constraint on highlights is short and demanding: a clinician
must be able to accept or reject a suggestion *quickly*, and every highlight
must carry a short reason and a pointer to its source. Both shape this module.

**Accept and reject are single POSTs with no body.** Nothing to fill in, nothing
to confirm. That is not laziness about the API — it is the whole reason Phase 4
has a learning signal to work with. A confirmation dialog on a suggestion a
clinician glanced at for half a second means the suggestion gets ignored
instead, and an ignored suggestion teaches the system nothing.

**Rejection is remembered, not just applied.** A rejected highlight stays as a
row with `status=rejected` rather than being deleted, so regeneration knows not
to propose it again, and Phase 4 can read it as a negative signal. Deleting it
would make the system re-suggest the same thing tomorrow, which is the fastest
way to train a clinician to stop reading the card.

**Provenance resolution is a real endpoint, not a link the client constructs.**
`GET /provenance?pointer=...` runs the pointer through the same resolver the
tests assert against, clinic-scoped, so a pointer can never be used to read
across a tenancy boundary even if the string is well-formed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.audit_logging import log_event
from app.core.enums import (
    AI_SCRIBED_TYPES,
    EntryType,
    HighlightStatus,
    InteractionAction,
    Role,
)
from app.core.provenance import ProvenanceError, resolve
from app.core.timeutil import UtcDateTime
from app.models import AuditLog, Entry, Highlight, Patient
from app.security import policy
from app.security.rbac import AccessScope, require_access
from app.services import highlights as highlight_service
from app.services import scoring
from app.services.interactions import record_interaction

router = APIRouter(tags=["highlights"])

# Staff read the Glance View too, but the brief makes accepting or rejecting a
# clinical judgement — so it is a clinician affordance (policy.CAN_DECIDE_HIGHLIGHTS).
VIEWING_ROLES = (Role.STAFF, Role.CLINICIAN, Role.ADMIN)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class HighlightOut(BaseModel):
    id: str
    entry_id: str
    patient_id: str
    span_start: int
    span_end: int
    span_text: str
    risk_reason: str
    provenance_pointer: str
    status: str
    score: float
    score_breakdown: dict[str, float]
    feature_tags: list[str]
    created_by: str
    created_by_role: str
    is_manual: bool
    stale: bool
    stale_reason: str | None = None
    source_version_number: int
    entry_type: str
    entry_title: str | None
    entry_timestamp: UtcDateTime
    is_ai_scribed: bool
    can_decide: bool
    decided_by: str | None
    decided_at: UtcDateTime | None


class ManualHighlightCreate(BaseModel):
    span_start: int = Field(ge=0)
    span_end: int = Field(ge=0)
    risk_reason: str | None = Field(default=None, max_length=300)


class ProvenanceOut(BaseModel):
    pointer: str
    kind: str
    entry_id: str | None = None
    session_id: str | None = None
    span: list[int] | None = None
    span_text: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


def _highlight_out(
    scope: AccessScope, highlight: Highlight, entry: Entry
) -> HighlightOut:
    return HighlightOut(
        id=highlight.id,
        entry_id=highlight.entry_id,
        patient_id=highlight.patient_id,
        span_start=highlight.span_start,
        span_end=highlight.span_end,
        span_text=highlight_service.anchored_text(scope.db, highlight, entry),
        risk_reason=highlight.risk_reason,
        provenance_pointer=highlight.provenance_pointer,
        status=str(highlight.status),
        score=round(highlight.score, 3),
        score_breakdown=scoring.decode_breakdown(highlight.score_breakdown),
        feature_tags=highlight_service.decode_tags(highlight.feature_tags),
        created_by=highlight.created_by,
        created_by_role=str(highlight.created_by_role),
        is_manual=highlight.created_by_role != Role.SYSTEM,
        stale=highlight_service.is_stale(highlight, entry),
        stale_reason=highlight_service.stale_reason(highlight, entry),
        source_version_number=highlight.source_version_number,
        entry_type=str(entry.type),
        entry_title=entry.title,
        entry_timestamp=entry.timestamp,
        is_ai_scribed=EntryType(entry.type) in AI_SCRIBED_TYPES,
        can_decide=policy.can_decide_highlights(scope.role),
        decided_by=highlight.decided_by,
        decided_at=highlight.decided_at,
    )


def _entry_for(scope: AccessScope, highlight: Highlight) -> Entry:
    entry = scope.get_or_404(Entry, highlight.entry_id)
    scope.assert_can_view_type(entry.type)
    return entry


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@router.get("/patients/{patient_id}/highlights", response_model=list[HighlightOut])
def list_highlights(
    patient_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    scope: AccessScope = Depends(require_access(*VIEWING_ROLES)),
) -> list[HighlightOut]:
    scope.get_or_404(Patient, patient_id)

    query = scope.query(Highlight).filter(Highlight.patient_id == patient_id)
    if status_filter:
        query = query.filter(Highlight.status == status_filter)
    rows = query.order_by(Highlight.score.desc()).all()

    out: list[HighlightOut] = []
    for row in rows:
        entry = scope.query(Entry).filter(Entry.id == row.entry_id).first()
        # A highlight on an entry type this role cannot read is not shown at
        # all. Otherwise the Glance View would quote a clinician section to a
        # member of staff who is refused the entry itself.
        if entry is None or not policy.can_view_type(scope.role, EntryType(entry.type)):
            continue
        out.append(_highlight_out(scope, row, entry))
    return out


@router.post("/patients/{patient_id}/highlights/refresh", response_model=list[HighlightOut])
def refresh_highlights(
    patient_id: str,
    scope: AccessScope = Depends(require_access(Role.CLINICIAN, Role.ADMIN)),
) -> list[HighlightOut]:
    """Rescore this patient's chart on demand.

    Normally unnecessary — scoring happens on write — but recency moves with
    the clock, and Phase 4's learned weights move without any entry changing.
    Exposing it explicitly is also what makes the learning demonstrable in a
    demo rather than something you have to wait for.
    """
    patient = scope.get_or_404(Patient, patient_id)
    highlight_service.refresh_patient_highlights(scope.db, patient.id, patient.clinic_id)
    scope.db.commit()
    return list_highlights(patient_id=patient_id, status_filter=None, scope=scope)


# --------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------


@router.post("/highlights/{highlight_id}/accept", response_model=HighlightOut)
def accept_highlight(
    highlight_id: str, scope: AccessScope = Depends(require_access(Role.CLINICIAN))
) -> HighlightOut:
    return _decide(scope, highlight_id, HighlightStatus.ACCEPTED)


@router.post("/highlights/{highlight_id}/reject", response_model=HighlightOut)
def reject_highlight(
    highlight_id: str, scope: AccessScope = Depends(require_access(Role.CLINICIAN))
) -> HighlightOut:
    return _decide(scope, highlight_id, HighlightStatus.REJECTED)


def _decide(
    scope: AccessScope, highlight_id: str, decision: HighlightStatus
) -> HighlightOut:
    highlight = scope.get_or_404(Highlight, highlight_id)
    entry = _entry_for(scope, highlight)

    highlight.status = decision
    highlight.decided_by = scope.user_id
    highlight.decided_at = _now()

    scope.db.add(
        AuditLog(
            actor_id=scope.user_id,
            actor_role=scope.role,
            clinic_id=scope.clinic_id,
            action=f"highlight.{decision}",
            target_type="highlight",
            target_id=highlight.id,
            audit_metadata=json.dumps(
                {
                    "entry_id": highlight.entry_id,
                    "entry_type": str(entry.type),
                    "score": round(highlight.score, 3),
                    "source_version": highlight.source_version_number,
                }
            ),
        )
    )
    # The strongest signal Phase 4 gets: a clinician looked at a specific
    # suggestion and said yes or no. Accept reinforces the tags, reject dampens.
    # record_interaction() folds this into FeatureWeight before it returns.
    record_interaction(
        scope.db,
        user_id=scope.user_id,
        user_role=scope.role,
        clinic_id=scope.clinic_id,
        action=(
            InteractionAction.ACCEPT_HIGHLIGHT
            if decision is HighlightStatus.ACCEPTED
            else InteractionAction.REJECT_HIGHLIGHT
        ),
        target_type="highlight",
        target_id=highlight.id,
        tags=highlight_service.decode_tags(highlight.feature_tags),
    )
    # Weights just moved, so every other highlight for this patient is now
    # scored against stale numbers. Rescoring here rather than on read keeps the
    # Glance View's 300ms budget intact, and it is what makes the learning
    # visible in the same breath as the click: confirming one warfarin
    # suggestion lifts the other warfarin content on the card immediately,
    # rather than at some later write nobody connects to the decision.
    highlight_service.refresh_patient_highlights(
        scope.db, highlight.patient_id, scope.clinic_id
    )
    scope.db.commit()
    scope.db.refresh(highlight)

    log_event(
        actor_id=scope.user_id,
        action=f"highlight.{decision}",
        target_type="highlight",
        target_id=highlight.id,
        clinic_id=scope.clinic_id,
        metadata={"entry_id": highlight.entry_id, "is_ai": str(entry.author_role) == "system"},
    )
    return _highlight_out(scope, highlight, entry)


@router.post(
    "/entries/{entry_id}/highlights",
    response_model=HighlightOut,
    status_code=status.HTTP_201_CREATED,
)
def create_manual_highlight(
    entry_id: str,
    payload: ManualHighlightCreate,
    scope: AccessScope = Depends(require_access(Role.CLINICIAN)),
) -> HighlightOut:
    """A clinician marking a phrase by hand — including inside an AI-scribed note.

    Note that this is allowed on entry types the clinician can *read* but not
    *write*: highlighting an AI summary is an annotation about it, not an edit
    of it, and the summary's text is untouched. That distinction is what lets a
    clinician engage with machine output without becoming its author.
    """
    entry = scope.get_or_404(Entry, entry_id)
    scope.assert_patient_visible(entry.patient_id)
    scope.assert_can_view_type(entry.type)

    try:
        highlight = highlight_service.create_manual_highlight(
            scope.db,
            entry=entry,
            span_start=payload.span_start,
            span_end=payload.span_end,
            created_by=scope.user_id,
            created_by_role=scope.role,
            risk_reason=payload.risk_reason,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    scope.db.add(
        AuditLog(
            actor_id=scope.user_id,
            actor_role=scope.role,
            clinic_id=scope.clinic_id,
            action="highlight.manual",
            target_type="highlight",
            target_id=highlight.id,
            audit_metadata=json.dumps(
                {
                    "entry_id": entry.id,
                    "entry_type": str(entry.type),
                    "span_length": payload.span_end - payload.span_start,
                    "source_version": entry.version_number,
                }
            ),
        )
    )
    record_interaction(
        scope.db,
        user_id=scope.user_id,
        user_role=scope.role,
        clinic_id=scope.clinic_id,
        action=InteractionAction.MANUAL_HIGHLIGHT,
        target_type="entry",
        target_id=entry.id,
        tags=highlight_service.decode_tags(highlight.feature_tags),
    )
    # A hand-marked phrase is the strongest evidence the learner gets, so the
    # rest of the chart is rescored against the weights it just moved.
    highlight_service.refresh_patient_highlights(
        scope.db, entry.patient_id, scope.clinic_id
    )
    scope.db.commit()
    scope.db.refresh(highlight)

    log_event(
        actor_id=scope.user_id,
        action="highlight.manual",
        target_type="highlight",
        target_id=highlight.id,
        clinic_id=scope.clinic_id,
        metadata={"entry_id": entry.id, "is_ai": str(entry.author_role) == "system"},
    )
    return _highlight_out(scope, highlight, entry)


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


@router.get("/provenance", response_model=ProvenanceOut)
def resolve_provenance(
    pointer: str = Query(min_length=3),
    scope: AccessScope = Depends(require_access()),
) -> ProvenanceOut:
    """Resolve a provenance pointer to its source, within this clinic.

    The clinic id passed here comes from the verified token, so a well-formed
    pointer to another clinic's entry resolves to a refusal rather than to data.
    A pointer is a reference, never an authorisation.
    """
    try:
        resolved = resolve(scope.db, pointer, clinic_id=scope.clinic_id)
    except ProvenanceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    entry_id = resolved.get("entry_id")
    if entry_id:
        entry = scope.query(Entry).filter(Entry.id == entry_id).first()
        if entry is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        # Resolving must obey the same type rules as reading. Otherwise the
        # pointer becomes a side door into content the role is refused head-on.
        scope.assert_patient_visible(entry.patient_id)
        scope.assert_can_view_type(entry.type)

    span = resolved.get("span")
    return ProvenanceOut(
        pointer=pointer,
        kind=str(resolved.get("kind")),
        entry_id=entry_id,
        session_id=resolved.get("session_id"),
        span=list(span) if span else None,
        span_text=resolved.get("span_text") or resolved.get("text"),
        detail={
            key: str(value)
            for key, value in resolved.items()
            if key not in {"span", "span_text", "text"}
        },
    )
