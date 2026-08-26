"""Phase 1, task 5 — cross-role reads are refused server-side.

Every request below goes straight at the HTTP API with a valid token for the
wrong role. Nothing here touches the UI, because the UI is not the control: a
page that hides clinician sections from staff is worth nothing if the API hands
one over when asked by id. That is the exact attack this file rules out.

"Rejected" is proved two ways, and both matter:

  * the direct fetch returns 403 — the caller is told no; and
  * the timeline listing does not contain the row at all — the caller is not
    even told it exists.

A build that only did the first would leak the existence, timestamp and author
of every clinician section to staff through the list endpoint.
"""

from __future__ import annotations

import pytest

from app.core.enums import Role


# --------------------------------------------------------------------------
# Patient: the most restricted role
# --------------------------------------------------------------------------


def test_patient_cannot_fetch_clinician_section_by_id(client_p1, token_for) -> None:
    """The headline check: a patient token asks for a clinician-only entry by
    its exact id, bypassing the UI entirely."""
    headers = token_for("u-a-patient", Role.PATIENT, "clinic-a", patient_id="patient-a1")
    response = client_p1.get("/entries/entry-a1-clin", headers=headers)

    assert response.status_code == 403
    # And the refusal must not leak the thing it is refusing.
    assert "HbA1c" not in response.text
    assert "microalbuminuria" not in response.text


def test_patient_cannot_fetch_staff_note_by_id(client_p1, token_for) -> None:
    headers = token_for("u-a-patient", Role.PATIENT, "clinic-a", patient_id="patient-a1")
    response = client_p1.get("/entries/entry-a1-staff", headers=headers)
    assert response.status_code == 403
    assert "138/86" not in response.text


def test_patient_cannot_fetch_raw_ai_note_by_id(client_p1, token_for) -> None:
    """The brief is explicit: patients see summaries and instructions, not raw
    AI-scribed notes."""
    headers = token_for("u-a-patient", Role.PATIENT, "clinic-a", patient_id="patient-a1")
    assert client_p1.get("/entries/entry-a1-ai", headers=headers).status_code == 403


def test_patient_timeline_omits_internal_entries_entirely(client_p1, token_for) -> None:
    """Not merely refused on fetch — absent from the listing, so the patient
    cannot learn that a clinician section exists, when it was written, or by
    whom."""
    headers = token_for("u-a-patient", Role.PATIENT, "clinic-a", patient_id="patient-a1")
    response = client_p1.get("/patients/patient-a1/entries", headers=headers)

    assert response.status_code == 200
    returned = {e["type"] for e in response.json()}
    assert returned == {"patient_note", "patient_instruction"}
    assert "entry-a1-clin" not in response.text
    assert "entry-a1-staff" not in response.text
    assert "entry-a1-ai" not in response.text


# --------------------------------------------------------------------------
# Staff: D-004, the documented least-privilege assumption
# --------------------------------------------------------------------------


def test_staff_cannot_fetch_clinician_section_by_id(client_p1, token_for) -> None:
    """D-004. The brief says clinicians may read staff notes and is silent on
    the reverse; this build denies it. If that assumption is ever reversed,
    this test is the thing that must change with it."""
    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    response = client_p1.get("/entries/entry-a1-clin", headers=headers)
    assert response.status_code == 403
    assert "HbA1c" not in response.text


def test_staff_timeline_omits_clinician_sections(client_p1, token_for) -> None:
    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    body = client_p1.get("/patients/patient-a1/entries", headers=headers).json()
    assert "clinician_section" not in {e["type"] for e in body}


def test_clinician_can_read_staff_notes(client_p1, token_for) -> None:
    """The asymmetry is intentional and stated, not an accident of the matrix.
    Asserting the permitted direction keeps D-004 honest: it shows the denial
    above is a policy choice, not a broken query."""
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    assert client_p1.get("/entries/entry-a1-staff", headers=headers).status_code == 200


# --------------------------------------------------------------------------
# Cross-role WRITES
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "user_id,role,entry_type",
    [
        ("u-a-staff", Role.STAFF, "clinician_section"),
        ("u-a-clinician", Role.CLINICIAN, "staff_note"),
        ("u-a-patient", Role.PATIENT, "clinician_section"),
        ("u-a-patient", Role.PATIENT, "staff_note"),
    ],
)
def test_no_role_can_author_as_another(client_p1, token_for, user_id, role, entry_type) -> None:
    """"Cannot overwrite the other's notes" starts here: you cannot author in
    someone else's type in the first place."""
    headers = token_for(user_id, role, "clinic-a", patient_id="patient-a1")
    response = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": entry_type, "content": "attempting to write outside my role"},
    )
    assert response.status_code == 403


def test_admin_cannot_author_clinical_content(client_p1, token_for) -> None:
    """Admin is clinic-scoped oversight, not authorship, so an admin account
    cannot quietly alter the clinical record."""
    headers = token_for("u-a-admin", Role.ADMIN, "clinic-a")
    response = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "staff_note", "content": "admin writing clinical content"},
    )
    assert response.status_code == 403
    # But oversight reads still work — the restriction is on writing only.
    assert client_p1.get("/entries/entry-a1-clin", headers=headers).status_code == 200


def test_human_cannot_forge_an_ai_scribed_entry(client_p1, token_for) -> None:
    """AI-scribed entries carry author_role=system and must come from the
    Phase 2 scribe pipeline, which routes through redaction. If a clinician
    could POST one, a client could fabricate machine provenance — and the
    provenance trail is the product's trust claim."""
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    response = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "ai_doctor_consult_summary", "content": "fabricated machine output"},
    )
    assert response.status_code == 403


def test_author_identity_is_taken_from_the_token_not_the_body(client_p1, token_for) -> None:
    """Spoofing authorship by sending author_id/author_role in the payload must
    have no effect. The extra keys are simply not part of the write model."""
    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    response = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={
            "type": "staff_note",
            "content": "Follow-up call completed.",
            "author_id": "u-a-clinician",
            "author_role": "clinician",
            "clinic_id": "clinic-b",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["author_id"] == "u-a-staff"
    assert body["author_role"] == "staff"


# --------------------------------------------------------------------------
# Patient-to-patient, within one clinic
# --------------------------------------------------------------------------


def test_patient_cannot_read_another_patients_timeline(client_p1, token_for) -> None:
    """Same clinic, different person. The clinic filter alone would let this
    through — it is the role dimension that catches it, which is why the two
    are fused."""
    headers = token_for("u-a-patient", Role.PATIENT, "clinic-a", patient_id="patient-a1")
    response = client_p1.get("/patients/patient-a2/entries", headers=headers)
    assert response.status_code == 403


def test_patient_patient_list_returns_only_self(client_p1, token_for) -> None:
    headers = token_for("u-a-patient", Role.PATIENT, "clinic-a", patient_id="patient-a1")
    body = client_p1.get("/patients", headers=headers).json()
    assert [p["id"] for p in body] == ["patient-a1"]
