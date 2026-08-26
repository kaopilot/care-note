"""Phase 1, task 6 — cross-clinic access is refused server-side.

Separate from the cross-role file on purpose. These are two independent
dimensions, and a build can get one right while getting the other wrong: a
clinician who legitimately reads clinician sections all day is *exactly* the
caller who must still be refused another clinic's clinician section. Testing
role and clinic in one file invites checking one and assuming the other.

Every request here carries a fully valid token for a role that would be
permitted if the row were in its own clinic. The only thing wrong is the
tenancy.

On the choice of 404 over 403
-----------------------------
Cross-clinic misses return 404, not 403. 403 means "this exists and you may not
have it", which turns the endpoint into an oracle: an attacker enumerates ids
and learns which patients exist at other clinics without ever reading one. 404
tells them nothing. The tests below assert 404 specifically, so the distinction
cannot be lost in a later refactor.
"""

from __future__ import annotations

import pytest

from app.core.enums import Role


# --------------------------------------------------------------------------
# Reads across the boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "user_id,role",
    [
        ("u-a-clinician", Role.CLINICIAN),
        ("u-a-staff", Role.STAFF),
        ("u-a-admin", Role.ADMIN),
    ],
)
def test_clinic_a_cannot_read_clinic_b_patient(client_p1, token_for, user_id, role) -> None:
    """The headline check, run for every role that has legitimate patient
    access within its own clinic."""
    headers = token_for(user_id, role, "clinic-a")
    response = client_p1.get("/patients/patient-b1", headers=headers)

    assert response.status_code == 404
    assert "Daniel Choo" not in response.text
    assert "MRN-B-88301" not in response.text


@pytest.mark.parametrize(
    "user_id,role",
    [("u-b-clinician", Role.CLINICIAN), ("u-b-staff", Role.STAFF), ("u-b-admin", Role.ADMIN)],
)
def test_clinic_b_cannot_read_clinic_a_patient(client_p1, token_for, user_id, role) -> None:
    """The converse. Phase 0 could only prove one direction because clinic B
    had no staff or admin; the Phase 1 seed makes B a full mirror so 'it only
    works one way round' is ruled out rather than hoped about."""
    headers = token_for(user_id, role, "clinic-b")
    assert client_p1.get("/patients/patient-a1", headers=headers).status_code == 404


def test_cross_clinic_timeline_is_refused(client_p1, token_for) -> None:
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    response = client_p1.get("/patients/patient-b1/entries", headers=headers)
    assert response.status_code == 404
    assert "Warfarin" not in response.text
    assert "INR" not in response.text


def test_cross_clinic_entry_fetch_by_id_is_refused(client_p1, token_for) -> None:
    """A clinician reading a clinician_section is normally allowed — the role
    dimension is satisfied here. Only the clinic dimension refuses this, which
    is precisely why it needs its own proof."""
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    response = client_p1.get("/entries/entry-b1-clin", headers=headers)
    assert response.status_code == 404
    assert "Warfarin" not in response.text


def test_patient_list_is_narrowed_to_own_clinic(client_p1, token_for) -> None:
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    ids = [p["id"] for p in client_p1.get("/patients", headers=headers).json()]
    assert set(ids) == {"patient-a1", "patient-a2"}
    assert not any(i.startswith("patient-b") for i in ids)


def test_patient_login_cannot_reach_the_other_clinic(client_p1, token_for) -> None:
    """Both dimensions are wrong at once. Must fail on either — asserting the
    set rather than one code, since which check fires first is an
    implementation detail that a refactor may legitimately change."""
    headers = token_for("u-a-patient", Role.PATIENT, "clinic-a", patient_id="patient-a1")
    assert client_p1.get("/patients/patient-b1", headers=headers).status_code in (403, 404)


# --------------------------------------------------------------------------
# Writes across the boundary
# --------------------------------------------------------------------------


def test_cannot_write_an_entry_into_another_clinic(client_p1, token_for) -> None:
    """A write is the more damaging direction: a leaked read is a privacy
    breach, but a cross-clinic write puts false content in a stranger's medical
    record."""
    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    response = client_p1.post(
        "/patients/patient-b1/entries",
        headers=headers,
        json={"type": "staff_note", "content": "should never land in clinic B"},
    )
    assert response.status_code == 404


def test_new_entries_are_stamped_with_the_token_clinic(client_p1, token_for, seeded_p1) -> None:
    """clinic_id on a written row comes from the token. Supplying a different
    one in the body must not change where the row lands."""
    from app.models import Entry

    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    response = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={
            "type": "staff_note",
            "content": "Routine follow-up.",
            "clinic_id": "clinic-b",
        },
    )
    assert response.status_code == 201

    stored = seeded_p1["db"].get(Entry, response.json()["id"])
    assert stored.clinic_id == "clinic-a"


# --------------------------------------------------------------------------
# The token is the only source of clinic
# --------------------------------------------------------------------------


def test_clinic_cannot_be_widened_by_query_parameter(client_p1, token_for) -> None:
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    ids = [
        p["id"] for p in client_p1.get("/patients?clinic_id=clinic-b", headers=headers).json()
    ]
    assert set(ids) == {"patient-a1", "patient-a2"}


def test_clinic_cannot_be_widened_by_header(client_p1, token_for) -> None:
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    headers["X-Clinic-Id"] = "clinic-b"
    ids = [p["id"] for p in client_p1.get("/patients", headers=headers).json()]
    assert set(ids) == {"patient-a1", "patient-a2"}


def test_forged_clinic_claim_needs_the_signing_key(client_p1) -> None:
    """The clinic claim is only trustworthy because the token is signed. A
    token minted with the wrong key must be rejected outright — otherwise
    'clinic_id comes from the token' would be worth nothing."""
    import jwt

    forged = jwt.encode(
        {"sub": "u-a-clinician", "role": "clinician", "clinic_id": "clinic-b"},
        "not-the-real-secret",
        algorithm="HS256",
    )
    response = client_p1.get("/patients", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
