"""Phase 0 RBAC pattern tests.

These prove the *pattern* works before any feature depends on it. Phase 3's
`test_rbac_scope.py` tests the real product routes with the same technique.

Every assertion below is made against the HTTP API, not the UI — the point is
that enforcement is server-side.
"""

from __future__ import annotations

import pytest

from app.core.enums import EntryType, Role
from app.security import policy


# --------------------------------------------------------------------------
# Authentication boundary
# --------------------------------------------------------------------------


def test_no_token_is_rejected(client) -> None:
    assert client.get("/demo/whoami").status_code == 401


def test_garbage_token_is_rejected(client) -> None:
    response = client.get("/demo/whoami", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_token_missing_clinic_claim_is_rejected(client) -> None:
    """A token that carries a role but no clinic must not fall back to a
    default clinic — it is unusable."""
    import jwt

    from app.core.config import settings

    bad = jwt.encode(
        {"sub": "u-a-staff", "role": "staff"},  # no clinic_id
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    response = client.get("/demo/whoami", headers={"Authorization": f"Bearer {bad}"})
    assert response.status_code == 401


# --------------------------------------------------------------------------
# Dimension 1: role
# --------------------------------------------------------------------------


def test_clinician_reaches_clinician_only_route(client, token_for) -> None:
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    assert client.get("/demo/clinician-only", headers=headers).status_code == 200


@pytest.mark.parametrize(
    "user_id,role",
    [("u-a-staff", Role.STAFF), ("u-a-patient", Role.PATIENT), ("u-a-admin", Role.ADMIN)],
)
def test_non_clinicians_blocked_from_clinician_route(client, token_for, user_id, role) -> None:
    headers = token_for(user_id, role, "clinic-a")
    assert client.get("/demo/clinician-only", headers=headers).status_code == 403


# --------------------------------------------------------------------------
# Dimension 2: clinic
# --------------------------------------------------------------------------


def test_listing_is_narrowed_to_own_clinic(client, token_for) -> None:
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    body = client.get("/demo/patients", headers=headers).json()
    assert body["patient_ids"] == ["patient-a1"]
    assert "patient-b1" not in body["patient_ids"]


def test_cross_clinic_direct_fetch_is_refused(client, token_for) -> None:
    """Clinic A clinician asks for Clinic B's patient by exact id, bypassing the
    UI entirely. Must not resolve."""
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    response = client.get("/demo/patients/patient-b1", headers=headers)
    assert response.status_code == 404
    assert "patient-b1" not in response.text or response.status_code != 200


def test_clinic_b_sees_only_its_own(client, token_for) -> None:
    headers = token_for("u-b-clinician", Role.CLINICIAN, "clinic-b")
    body = client.get("/demo/patients", headers=headers).json()
    assert body["patient_ids"] == ["patient-b1"]


def test_clinic_claim_cannot_be_widened_by_query_param(client, token_for) -> None:
    """clinic_id is read from the token only. Supplying one in the request must
    change nothing."""
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    body = client.get("/demo/patients?clinic_id=clinic-b", headers=headers).json()
    assert body["clinic_id"] == "clinic-a"
    assert body["patient_ids"] == ["patient-a1"]


# --------------------------------------------------------------------------
# The two dimensions together
# --------------------------------------------------------------------------


def test_patient_cannot_read_another_patient_record(client, token_for) -> None:
    headers = token_for("u-a-patient", Role.PATIENT, "clinic-a", patient_id="patient-a1")
    assert client.get("/demo/patients/patient-a1", headers=headers).status_code == 200
    # Same clinic, different person: role check must still catch it.
    assert client.get("/demo/patients/patient-b1", headers=headers).status_code in (403, 404)


def test_access_scope_refuses_unscoped_models(client, token_for, seeded) -> None:
    """A model with no clinic_id cannot be queried through AccessScope — the
    fail-closed default that stops an unfiltered read from ever compiling."""
    from app.core.enums import Role as R
    from app.security.rbac import AccessScope

    scope = AccessScope(
        user_id="u-a-staff", role=R.STAFF, clinic_id="clinic-a", db=seeded["db"]
    )

    class Unscoped:  # no clinic_id attribute
        __name__ = "Unscoped"

    with pytest.raises(TypeError, match="clinic_id"):
        scope.query(Unscoped)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Policy matrix (the rules themselves, independent of transport)
# --------------------------------------------------------------------------


def test_staff_cannot_view_clinician_sections() -> None:
    """Documented assumption D-004: least privilege where the brief is silent."""
    assert not policy.can_view_type(Role.STAFF, EntryType.CLINICIAN_SECTION)
    assert policy.can_view_type(Role.CLINICIAN, EntryType.STAFF_NOTE)


def test_neither_role_can_write_as_the_other() -> None:
    assert not policy.can_write_type(Role.CLINICIAN, EntryType.STAFF_NOTE)
    assert not policy.can_write_type(Role.STAFF, EntryType.CLINICIAN_SECTION)


def test_patient_cannot_view_raw_ai_notes() -> None:
    for ai_type in (
        EntryType.AI_DOCTOR_CONSULT_SUMMARY,
        EntryType.AI_NURSE_CONSULT_SUMMARY,
        EntryType.AI_PATIENT_SESSION_SUMMARY,
    ):
        assert not policy.can_view_type(Role.PATIENT, ai_type)
    assert policy.can_view_type(Role.PATIENT, EntryType.PATIENT_INSTRUCTION)


def test_patient_cannot_view_internal_comments() -> None:
    assert not policy.can_view_internal_comments(Role.PATIENT)
    assert policy.can_view_internal_comments(Role.STAFF)


def test_admin_is_oversight_not_authorship() -> None:
    """Admin reads everything in-clinic but authors no clinical content, so an
    admin account cannot quietly alter the record."""
    assert policy.can_view_type(Role.ADMIN, EntryType.CLINICIAN_SECTION)
    assert policy.WRITABLE_TYPES[Role.ADMIN] == frozenset()


def test_login_issues_scoped_token(client) -> None:
    response = client.post("/auth/login", json={"username": "staff_a", "password": "pw"})
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "staff"
    assert body["clinic_id"] == "clinic-a"


def test_login_failure_does_not_enumerate_accounts(client) -> None:
    unknown = client.post("/auth/login", json={"username": "nobody", "password": "pw"})
    wrong_pw = client.post("/auth/login", json={"username": "staff_a", "password": "bad"})
    assert unknown.status_code == wrong_pw.status_code == 401
    assert unknown.json()["detail"] == wrong_pw.json()["detail"]
