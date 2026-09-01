"""The patient who exists as a phone number in a WhatsApp thread.

Scenario 1. The identity model was never the obstacle — there is no email column
anywhere in this schema and a phone number works as a username today. The
obstacle was that nothing could create the row: every account in the build
existed because a developer ran `init_db.py`.

Scenario 5 is the same absence one level up, and these tests pin the half that
belongs to routine clinic work: registering a patient and issuing a login.

See DECISIONS.md D-075.
"""

from __future__ import annotations

from app.models import Patient, User


def _login(client, username: str, password: str = "pw"):
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# --- the case from the review --------------------------------------------


def test_a_nurse_can_enrol_a_patient_who_only_has_a_phone_number(client, seeded):
    staff = _login(client, "staff_a")
    response = client.post(
        "/patients",
        headers=staff,
        json={
            "name": "Siti Nurhaliza",
            "identifier_type": "phone",
            "identifier": "+65 9123 4567",
            "create_login": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reachable"] is True
    assert body["username"] == "+6591234567"
    assert len(body["one_time_passcode"]) == 6


def test_she_can_then_actually_log_in(client, seeded):
    staff = _login(client, "staff_a")
    created = client.post(
        "/patients",
        headers=staff,
        json={
            "name": "Siti Nurhaliza",
            "identifier_type": "phone",
            "identifier": "0123456789",
            "create_login": True,
        },
    ).json()

    # The whole point: no email was involved at any stage.
    headers = _login(client, created["username"], created["one_time_passcode"])
    own_view = client.get(f"/patients/{created['patient_id']}/my-care", headers=headers)
    assert own_view.status_code == 200


def test_the_passcode_is_never_stored_in_readable_form(client, seeded, db_session):
    staff = _login(client, "staff_a")
    created = client.post(
        "/patients",
        headers=staff,
        json={"name": "Siti", "identifier_type": "phone",
              "identifier": "0123456789", "create_login": True},
    ).json()

    user = db_session.query(User).filter(User.username == created["username"]).one()
    assert created["one_time_passcode"] not in user.password_hash


# --- a login is optional --------------------------------------------------


def test_a_patient_can_be_enrolled_without_a_login(client, seeded):
    """Plenty of patients will never use a portal. Forcing a credential nobody
    wants produces dormant accounts; reporting unreachable is more honest."""
    staff = _login(client, "staff_a")
    body = client.post(
        "/patients",
        headers=staff,
        json={"name": "Mdm Tan", "identifier_type": "internal", "create_login": False},
    ).json()
    assert body["reachable"] is False
    assert body["one_time_passcode"] is None


def test_a_login_can_be_issued_later(client, seeded):
    staff = _login(client, "staff_a")
    created = client.post(
        "/patients",
        headers=staff,
        json={"name": "Mdm Tan", "identifier_type": "internal", "create_login": False},
    ).json()

    issued = client.post(
        f"/patients/{created['patient_id']}/login",
        json={"identifier": "0198887777"},
        headers=staff,
    )
    assert issued.status_code == 200
    assert len(issued.json()["one_time_passcode"]) == 6


# --- enrolment obeys the same scoping as every read ----------------------


def test_enrolment_puts_the_patient_in_the_callers_clinic(client, seeded, db_session):
    """clinic_id comes from the token, never the body."""
    staff = _login(client, "staff_a")
    created = client.post(
        "/patients",
        headers=staff,
        json={"name": "Someone", "identifier_type": "internal"},
    ).json()

    patient = db_session.query(Patient).filter(Patient.id == created["patient_id"]).one()
    assert patient.clinic_id == "clinic-a"


def test_a_patient_cannot_enrol_anyone(client, seeded):
    patient = _login(client, "patient_a")
    response = client.post(
        "/patients",
        headers=patient,
        json={"name": "Fake Person", "identifier_type": "internal"},
    )
    assert response.status_code == 403


def test_staff_cannot_issue_a_login_for_another_clinics_patient(client, seeded):
    """The enrolment path is scoped by the same rule as every read path."""
    staff_a = _login(client, "staff_a")
    response = client.post("/patients/patient-b1/login", headers=staff_a)
    assert response.status_code == 404


def test_a_duplicate_identifier_is_refused(client, seeded):
    staff = _login(client, "staff_a")
    payload = {
        "name": "First Person",
        "identifier_type": "phone",
        "identifier": "0111111111",
        "create_login": True,
    }
    assert client.post("/patients", headers=staff, json=payload).status_code == 201
    second = client.post(
        "/patients",
        headers=staff,
        json={**payload, "name": "Second Person"},
    )
    assert second.status_code == 409


def test_phone_validation_is_permissive_not_strict(client, seeded):
    """Strict validation is how you exclude the person this route exists for."""
    staff = _login(client, "staff_a")
    for number in ("+65 9123 4567", "012-345 6789", "0123456789"):
        response = client.post(
            "/patients",
            headers=staff,
            json={"name": "X", "identifier_type": "phone", "identifier": number},
        )
        assert response.status_code == 201, f"{number} was rejected"
