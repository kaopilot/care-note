"""Learned importance and the data-decay lifecycle.

Two Phase 4 surfaces, and they share a theme: both make something the system
does *to* the record inspectable by the people responsible for it.

`GET /clinic/learning` exposes what the ranking has learned from this clinic's
behaviour — which tags it now promotes, which it dampens, and how many signals
sit behind each. The brief's framing is that clinicians trust machine output
only up to a point and then need reassurance. A ranker that quietly changes
under them is the failure mode that framing warns about, so the learned state
is readable by any clinical role, and every row carries its evidence count
rather than only a number.

The decay routes default to a dry run. Compression is the one operation here
that rewrites stored clinical text, so the default behaviour of the endpoint is
to describe what it would do and change nothing.

Scoping is the usual one: `AccessScope` fuses role and clinic, so a clinician in
clinic A reads clinic A's learned weights and cannot address clinic B's at all.
That is not decoration — one clinic's attention habits leaking into another's
prioritisation would be a tenancy breach expressed as a ranking.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.audit_logging import log_event
from app.core.enums import DecayState, Role
from app.core.timeutil import iso_utc
from app.models import AuditLog, Entry, EntryArchive
from app.routes.schemas import EntryOut, entry_out
from app.security.rbac import AccessScope, require_access
from app.services import decay as decay_service
from app.services import highlights as highlight_service
from app.services import learning

router = APIRouter(tags=["learning"])

CLINICAL_ROLES = (Role.STAFF, Role.CLINICIAN, Role.ADMIN)


class LearningOut(BaseModel):
    clinic_id: str
    weights: list[dict[str, Any]]
    signal_counts: dict[str, int]
    policy: dict[str, Any]


# --------------------------------------------------------------------------
# Learned weights
# --------------------------------------------------------------------------


def _rescore_clinic(scope: AccessScope) -> None:
    """Rescore every highlight in the clinic after learned weights move.

    Scores are precomputed on write so the Glance View stays inside its 300ms
    budget (services/glance.py). A learned weight moving is a write that no
    entry knows about, so it is the one case where the rescore has to be driven
    from here rather than from an entry edit.
    """
    patients = {
        entry.patient_id
        for entry in scope.db.query(Entry).filter(Entry.clinic_id == scope.clinic_id).all()
    }
    for patient_id in patients:
        highlight_service.refresh_patient_highlights(scope.db, patient_id, scope.clinic_id)


@router.get("/clinic/learning", response_model=LearningOut)
def get_learning(
    limit: int = Query(default=12, ge=1, le=50),
    scope: AccessScope = Depends(require_access(*CLINICAL_ROLES)),
) -> LearningOut:
    """What this clinic's behaviour has taught the ranking.

    Contains feature tags and counts only — no patient identifiers, no entry
    references, no content. The learning substrate is deliberately built from
    vocabulary rather than prose (SCHEMA.md), which is what makes it safe to
    show a whole clinic at once.
    """
    return LearningOut(
        clinic_id=scope.clinic_id,
        weights=learning.top_weights(scope.db, scope.clinic_id, limit=limit),
        signal_counts=learning.signal_summary(scope.db, scope.clinic_id),
        policy={
            "half_life_days": learning.SIGNAL_HALF_LIFE_DAYS,
            "saturation": learning.SATURATION,
            "learning_roles": sorted(learning.LEARNING_ROLES),
            "never_dampened": sorted(learning.NEVER_DAMPENED),
            "action_signal": learning.ACTION_SIGNAL,
        },
    )


@router.post("/clinic/learning/rebuild", response_model=LearningOut)
def rebuild_learning(
    scope: AccessScope = Depends(require_access(Role.CLINICIAN, Role.ADMIN)),
) -> LearningOut:
    """Recompute every learned weight for this clinic from the interaction log.

    Safe to call at any time: `FeatureWeight` is a materialised view over
    `InteractionLog`, so this is idempotent and must reproduce exactly what the
    incremental updates produced. It exists because evidence decays with the
    clock — weights drift downward over time with nobody touching anything, and
    a demo should be able to force that forward rather than wait a quarter.
    """
    weights = learning.rebuild_clinic(scope.db, scope.clinic_id)
    _rescore_clinic(scope)
    scope.db.commit()

    log_event(
        actor_id=scope.user_id,
        action="learning.rebuild",
        target_type="clinic",
        target_id=scope.clinic_id,
        clinic_id=scope.clinic_id,
        metadata={"tags": len(weights)},
    )
    return get_learning(limit=12, scope=scope)


# --------------------------------------------------------------------------
# Data decay
# --------------------------------------------------------------------------


@router.get("/clinic/decay/preview")
def preview_decay(
    scope: AccessScope = Depends(require_access(Role.CLINICIAN, Role.ADMIN)),
) -> dict[str, Any]:
    """What a decay pass would do to this clinic, without doing any of it."""
    return decay_service.run(scope.db, clinic_id=scope.clinic_id, dry_run=True)


@router.post("/clinic/decay/run")
def run_decay(
    dry_run: bool = Query(default=True),
    scope: AccessScope = Depends(require_access(Role.ADMIN)),
) -> dict[str, Any]:
    """Apply the decay policy across this clinic.

    Admin-only, and `dry_run=true` by default. Admin is the oversight role
    (D-011): it cannot author clinical content, which makes it the right holder
    of a lifecycle operation that rewrites stored text without adding any
    clinical claim to the record.
    """
    report = decay_service.run(scope.db, clinic_id=scope.clinic_id, dry_run=dry_run)

    if not dry_run:
        scope.db.add(
            AuditLog(
                actor_id=scope.user_id,
                actor_role=scope.role,
                clinic_id=scope.clinic_id,
                action="decay.run",
                target_type="clinic",
                target_id=scope.clinic_id,
                audit_metadata=json.dumps(
                    {
                        "changed": report["changed"],
                        "evaluated": report["evaluated"],
                        "hot_bytes_saved": report["hot_bytes_saved"],
                        "archive_bytes": report["archive_bytes"],
                    }
                ),
            )
        )
        scope.db.commit()

    log_event(
        actor_id=scope.user_id,
        action="decay.run",
        target_type="clinic",
        target_id=scope.clinic_id,
        clinic_id=scope.clinic_id,
        metadata={"dry_run": dry_run, "changed": report["changed"]},
    )
    return report


@router.post("/entries/{entry_id}/restore", response_model=EntryOut)
def restore_entry(
    entry_id: str,
    scope: AccessScope = Depends(require_access(Role.CLINICIAN, Role.ADMIN)),
) -> EntryOut:
    """Rehydrate a compressed entry to its full original text.

    Restoring is a read affordance, not an edit: it appends no `Version`,
    because nothing about the clinical content changed — the same words that
    were archived come back byte for byte. Recording it as a revision would put
    a storage-tier event into a clinical audit trail and make the version
    history harder to read for the thing it is actually for.
    """
    entry = scope.get_or_404(Entry, entry_id)
    scope.assert_patient_visible(entry.patient_id)
    scope.assert_can_view_type(entry.type)

    if str(entry.decay_state) != str(DecayState.COLD):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="entry is not archived",
        )
    if not decay_service.restore(scope.db, entry):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no archived copy found for this entry",
        )

    scope.db.add(
        AuditLog(
            actor_id=scope.user_id,
            actor_role=scope.role,
            clinic_id=scope.clinic_id,
            action="entry.restore",
            target_type="entry",
            target_id=entry.id,
            audit_metadata=json.dumps({"content_length": len(entry.content or "")}),
        )
    )
    highlight_service.refresh_entry_highlights(scope.db, entry)
    scope.db.commit()
    scope.db.refresh(entry)

    log_event(
        actor_id=scope.user_id,
        action="entry.restore",
        target_type="entry",
        target_id=entry.id,
        clinic_id=scope.clinic_id,
        metadata={"decay_state": str(entry.decay_state)},
    )
    return entry_out(entry, author_name=None)


@router.get("/entries/{entry_id}/archive")
def get_archive_metadata(
    entry_id: str,
    scope: AccessScope = Depends(require_access(*CLINICAL_ROLES)),
) -> dict[str, Any]:
    """Metadata about a compressed entry's stored original.

    Sizes and timestamps, not the text. A reader who wants the original asks
    for a restore, which is audited; letting this endpoint return the content
    would be a quiet way around that.
    """
    entry = scope.get_or_404(Entry, entry_id)
    scope.assert_patient_visible(entry.patient_id)
    scope.assert_can_view_type(entry.type)

    row = (
        scope.query(EntryArchive)
        .filter(EntryArchive.entry_id == entry.id)
        .order_by(EntryArchive.archived_at.desc())
        .first()
    )
    if row is None:
        return {"entry_id": entry.id, "archived": False, "decay_state": str(entry.decay_state)}
    return {
        "entry_id": entry.id,
        "archived": True,
        "decay_state": str(entry.decay_state),
        "compression": row.compression,
        "original_length": row.original_length,
        "stored_length": len(row.archived_content),
        "current_length": len(entry.content or ""),
        "archived_at": iso_utc(row.archived_at),
    }
