"""The Glance View, the patient's view of the same record, and the scribe trigger.

Two views over one record, with different jobs:

* `/patients/{id}/glance` is the clinical Top Card. Dense, ranked, and honest
  about which of it came from a machine.
* `/patients/{id}/my-care` is what the patient sees. Same underlying entries,
  filtered by the same server-side policy, but written in plain language and
  deliberately calmer. It is not scored on information density — it is scored on
  whether an anxious non-clinical reader knows what to do next.

The scribe routes live here rather than with entries because what they produce
is timeline content whose whole point is that a human did not write it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.audit_logging import log_event
from app.core.enums import InteractionType, Role
from app.models import AIScribedNote, Patient
from app.routes.schemas import EntryOut, entry_out
from app.security.rbac import AccessScope, require_access
from app.services import glance, scribe, transcripts

router = APIRouter(tags=["glance"])

CLINICAL_ROLES = (Role.STAFF, Role.CLINICIAN, Role.ADMIN)


class ScribeRequest(BaseModel):
    interaction_type: InteractionType
    # Regeneration targets an existing session rather than creating a new one.
    # Both fields are required together: regenerating without naming the session
    # would silently pick one (D-078).
    session_id: str | None = None
    regenerate: bool = False


@router.get("/patients/{patient_id}/glance")
def get_glance(
    patient_id: str, scope: AccessScope = Depends(require_access(*CLINICAL_ROLES))
) -> dict[str, Any]:
    """The Top Card. Reads precomputed scores; does no scoring itself.

    Response time is reported by the middleware in `X-Response-Time-Ms` on every
    request, which is how the latency figure in the technical brief was
    measured rather than estimated (see scripts/bench_glance.py).
    """
    scope.assert_patient_visible(patient_id)
    patient = scope.get_or_404(Patient, patient_id)

    payload = glance.build_glance(
        scope.db, role=scope.role, user_id=scope.user_id, patient=patient
    )
    log_event(
        actor_id=scope.user_id,
        action="glance.view",
        target_type="patient",
        target_id=patient.id,
        clinic_id=scope.clinic_id,
        metadata={
            "role": str(scope.role),
            "highlights": len(payload["highlights"]),
            "whats_new": payload["whats_new"]["count"],
        },
    )
    return payload


@router.get("/patients/{patient_id}/my-care")
def get_patient_view(
    patient_id: str, scope: AccessScope = Depends(require_access(Role.PATIENT))
) -> dict[str, Any]:
    """The patient's own view. Patient role only — by design.

    A clinician wanting to check what their patient sees should look at the
    `patient_instruction` and `patient_summary` entries in the timeline, which
    is the same content. Serving this endpoint to clinical roles would create a
    second, subtly different rendering of patient-facing text, and the two would
    drift.
    """
    scope.assert_patient_visible(patient_id)
    patient = scope.get_or_404(Patient, patient_id)

    payload = glance.build_patient_glance(
        scope.db, user_id=scope.user_id, patient=patient
    )
    log_event(
        actor_id=scope.user_id,
        action="patient_view.view",
        target_type="patient",
        target_id=patient.id,
        clinic_id=scope.clinic_id,
        metadata={"next_steps": len(payload["next_steps"])},
    )
    return payload


# --------------------------------------------------------------------------
# AI scribe
# --------------------------------------------------------------------------


@router.get("/scribe/templates")
def list_scribe_templates(
    scope: AccessScope = Depends(require_access()),
) -> list[dict[str, Any]]:
    """The synthetic transcripts available to run through the pipeline.

    Fixtures rather than live audio: Phase 5 replaces the transcription source
    without changing anything downstream of it, because the pipeline already
    consumes turn-structured input.
    """
    return transcripts.describe()


@router.post(
    "/patients/{patient_id}/scribe",
    response_model=EntryOut,
    status_code=status.HTTP_201_CREATED,
)
def run_scribe(
    patient_id: str,
    payload: ScribeRequest,
    scope: AccessScope = Depends(require_access()),
) -> EntryOut:
    """Run a transcript through redaction → LLM → structured summary → Entry.

    Who may trigger which kind of session is itself an access rule. A patient
    can generate their own pre-consult AI session summary — that is the brief's
    "patient-provided insight" — but cannot manufacture a doctor consult
    summary, which would let a patient login write words into the record
    attributed to a clinical encounter that never happened.
    """
    scope.assert_patient_visible(patient_id)
    patient = scope.get_or_404(Patient, patient_id)

    if scope.role is Role.PATIENT and (
        payload.interaction_type is not InteractionType.AI_PATIENT_SESSION
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Patients may only generate their own AI session summaries",
        )
    if scope.role is Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin is an oversight role and does not author clinical content",
        )

    try:
        entry = scribe.run_scribe(
            scope.db,
            patient=patient,
            interaction_type=payload.interaction_type,
            actor_id=scope.user_id,
            session_id=payload.session_id,
            regenerate=payload.regenerate,
        )
    except scribe.RegenerationRefused as exc:
        # 409, not 400. Nothing about the request is malformed — the record is
        # in a state where honouring it would destroy something. The message is
        # written for the clinician who will read it, and says what to do next.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "reason": exc.reason},
        ) from exc
    # Attach the provenance record to the response. The redaction count and the
    # confidence figure are the two things that make this entry auditable rather
    # than merely present, and the client shows both on the card.
    ai_note = (
        scope.query(AIScribedNote).filter(AIScribedNote.entry_id == entry.id).first()
    )
    return entry_out(entry, author_name="Care Note AI", ai_note=ai_note)
