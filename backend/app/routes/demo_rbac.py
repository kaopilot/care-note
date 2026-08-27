"""Routes that demonstrate the RBAC pattern in isolation (Phase 0, step 4).

These are NOT the product. They exist so the enforcement layer can be exercised
independently of any feature. Phase 1 added real patient/timeline routes using
the identical `require_access` pattern; these were kept alongside them.

RETAINED DELIBERATELY — see DECISIONS.md D-057. D-026 originally scheduled these
for deletion in Phase 3, which did not happen. The reason to keep them is that
`tests/test_rbac_pattern.py` (18 tests) proves role and clinic enforcement
against a surface with no product logic on it, so a failure there is
unambiguously an enforcement-layer failure rather than a feature bug. That is a
useful second opinion, and deleting the routes would delete those tests.

They are gated by the same `require_access` dependency as every product route
and expose nothing a product route would not. `/demo/whoami` returns only what
the caller's own token already asserts.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.enums import Role
from app.models import Patient
from app.security.rbac import AccessScope, require_access

router = APIRouter(prefix="/demo", tags=["demo-rbac"])


@router.get("/whoami")
def whoami(scope: AccessScope = Depends(require_access())) -> dict:
    """Any authenticated role. Shows what the caller is scoped to."""
    return {
        "user_id": scope.user_id,
        "role": str(scope.role),
        "clinic_id": scope.clinic_id,
        "viewable_entry_types": scope.viewable_types(),
    }


@router.get("/clinician-only")
def clinician_only(scope: AccessScope = Depends(require_access(Role.CLINICIAN))) -> dict:
    """Role dimension: a staff or patient token is rejected here with 403."""
    return {"ok": True, "message": "clinician-only payload", "user_id": scope.user_id}


@router.get("/patients")
def list_patients_in_scope(
    scope: AccessScope = Depends(require_access(Role.STAFF, Role.CLINICIAN, Role.ADMIN)),
) -> dict:
    """Clinic dimension: the clinic filter is applied by AccessScope.query,
    not by anything written in this handler. Nothing here mentions clinic_id."""
    patients = scope.query(Patient).all()
    return {
        "clinic_id": scope.clinic_id,
        "count": len(patients),
        "patient_ids": [p.id for p in patients],
    }


@router.get("/patients/{patient_id}")
def get_patient(
    patient_id: str,
    scope: AccessScope = Depends(require_access()),
) -> dict:
    """Both dimensions at once: a cross-clinic id 404s via `get_or_404`, and a
    patient login reading someone else's id is refused by
    `assert_patient_visible`."""
    scope.assert_patient_visible(patient_id)
    patient = scope.get_or_404(Patient, patient_id)
    return {"id": patient.id, "clinic_id": patient.clinic_id, "mrn": patient.mrn}
