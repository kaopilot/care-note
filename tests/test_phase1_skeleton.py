"""Phase 1 exit criteria that are not about refusal.

The two cross-* files prove what is *blocked*. This one proves the skeleton
actually carries weight: every seeded role can log in, a real entry can be
written through the API, and the same patient genuinely looks different to
different roles.

That last point is the one worth being careful about. "Each role sees a
correctly scoped view" is easy to satisfy vacuously — if the seed gives every
role the same rows, a completely broken filter still passes. So the assertions
below compare the roles *against each other* and require the views to differ.
"""

from __future__ import annotations

import time

import pytest

from app.core.enums import Role


# --------------------------------------------------------------------------
# Exit criterion: can log in as each of the four seeded roles
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "username,expected_role",
    [
        ("clinician_a", "clinician"),
        ("staff_a", "staff"),
        ("admin_a", "admin"),
        ("patient_a", "patient"),
    ],
)
def test_each_role_can_log_in(client_p1, username, expected_role) -> None:
    response = client_p1.post("/auth/login", json={"username": username, "password": "pw"})
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == expected_role
    assert body["clinic_id"] == "clinic-a"


@pytest.mark.parametrize(
    "username,expected_role",
    [
        ("clinician_b", "clinician"),
        ("staff_b", "staff"),
        ("admin_b", "admin"),
        ("patient_b", "patient"),
    ],
)
def test_clinic_b_is_a_full_mirror(client_p1, username, expected_role) -> None:
    """Clinic B has all four roles too, otherwise the cross-clinic proofs could
    only ever run in one direction."""
    body = client_p1.post("/auth/login", json={"username": username, "password": "pw"}).json()
    assert body["role"] == expected_role
    assert body["clinic_id"] == "clinic-b"


def test_patient_login_carries_its_patient_id(client_p1) -> None:
    """A patient token needs the extra `patient_id` claim; without it the
    'only your own record' rule has nothing to compare against."""
    client_p1.post("/auth/login", json={"username": "patient_a", "password": "pw"})
    me = client_p1.get("/auth/me").json()
    assert me["patient_id"] == "patient-a1"
    assert me["role"] == "patient"


def test_session_survives_without_the_token_in_javascript(client_p1) -> None:
    """The browser path end to end: log in, then make an authenticated request
    carrying nothing but the httpOnly cookie. This is what lets the frontend
    avoid localStorage entirely (D-016/D-020)."""
    client_p1.post("/auth/login", json={"username": "staff_a", "password": "pw"})
    response = client_p1.get("/patients")  # no Authorization header at all
    assert response.status_code == 200
    assert {p["id"] for p in response.json()} == {"patient-a1", "patient-a2"}


def test_logout_clears_the_session(client_p1) -> None:
    client_p1.post("/auth/login", json={"username": "staff_a", "password": "pw"})
    assert client_p1.get("/auth/me").status_code == 200
    client_p1.post("/auth/logout")
    assert client_p1.get("/auth/me").status_code == 401


# --------------------------------------------------------------------------
# Exit criterion: one real Entry, created through the API
# --------------------------------------------------------------------------


def test_entry_created_via_api_is_persisted_with_full_metadata(
    client_p1, token_for, seeded_p1
) -> None:
    """Task 3. Written through an authenticated HTTP call, not by poking the
    session, and carrying every field the shared context requires on a timeline
    entry."""
    from app.models import Entry

    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    response = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={
            "type": "staff_note",
            "title": "Phone follow-up",
            "content": "Discussed evening dose timing. Target BP <130/80.",
        },
    )
    assert response.status_code == 201
    body = response.json()

    # Required metadata, all present and server-assigned.
    assert body["author_role"] == "staff"
    assert body["author_id"] == "u-a-staff"
    assert body["type"] == "staff_note"
    assert body["timestamp"]
    assert body["provenance_pointer"] == f"entry://{body['id']}"
    assert body["is_ai_scribed"] is False

    stored = seeded_p1["db"].get(Entry, body["id"])
    assert stored is not None
    assert stored.clinic_id == "clinic-a"
    assert stored.version_number == 1
    assert stored.current_version_id is not None


def test_clinical_angle_brackets_are_stored_verbatim(client_p1, token_for) -> None:
    """D-015: content is never escaped or tag-stripped on write. `<130/80` must
    survive intact — silently corrupting a dose or a target is a patient-safety
    bug worse than the XSS that escaping would prevent, and the XSS is already
    handled at the render boundary."""
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    content = "Titrate if BP <130/80 and dose <5mg tolerated; sats <92% on RA."
    response = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "clinician_section", "content": content},
    )
    assert response.status_code == 201
    assert response.json()["content"] == content
    assert "&lt;" not in response.json()["content"]


def test_new_entry_appears_at_the_top_of_the_timeline(client_p1, token_for) -> None:
    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    created = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "staff_note", "content": "Most recent contact."},
    ).json()

    timeline = client_p1.get("/patients/patient-a1/entries", headers=headers).json()
    assert timeline[0]["id"] == created["id"]


def test_entry_creation_writes_an_audit_row_without_content(
    client_p1, token_for, seeded_p1
) -> None:
    """Revision history needs to know a write happened; it must not need the
    text to do so. The audit row carries ids, a type, a length and any injection
    markers — never the note."""
    import json

    from app.models import AuditLog

    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    secret = "Patient disclosed something sensitive in confidence."
    created = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "staff_note", "content": secret},
    ).json()

    row = (
        seeded_p1["db"]
        .query(AuditLog)
        .filter(AuditLog.target_id == created["id"])
        .one()
    )
    assert row.action == "entry.create"
    assert row.actor_id == "u-a-staff"
    assert secret not in row.audit_metadata
    assert json.loads(row.audit_metadata)["content_length"] == len(secret)


# --------------------------------------------------------------------------
# Exit criterion: each role sees a correctly scoped view of the SAME patient
# --------------------------------------------------------------------------


def _types_for(client, token_for, user_id, role, patient_id="patient-a1"):
    kwargs = {"patient_id": patient_id} if role is Role.PATIENT else {}
    headers = token_for(user_id, role, "clinic-a", **kwargs)
    response = client.get(f"/patients/{patient_id}/entries", headers=headers)
    assert response.status_code == 200
    return {e["type"] for e in response.json()}


def test_four_roles_see_four_correctly_scoped_views(client_p1, token_for) -> None:
    """The same patient, four logins, four different answers — and the
    differences are the policy, not an accident."""
    clinician = _types_for(client_p1, token_for, "u-a-clinician", Role.CLINICIAN)
    staff = _types_for(client_p1, token_for, "u-a-staff", Role.STAFF)
    admin = _types_for(client_p1, token_for, "u-a-admin", Role.ADMIN)
    patient = _types_for(client_p1, token_for, "u-a-patient", Role.PATIENT)

    # Clinician: everything on this patient.
    assert clinician == {
        "clinician_section",
        "staff_note",
        "patient_instruction",
        "patient_note",
        "ai_doctor_consult_summary",
    }
    # Staff: everything except the clinician's reasoning (D-004).
    assert staff == clinician - {"clinician_section"}
    # Admin: clinic-wide oversight, so the same read surface as the clinician.
    assert admin == clinician
    # Patient: patient-facing only. No internal notes, no raw AI output.
    assert patient == {"patient_instruction", "patient_note"}

    # The views must actually differ, or this test proves nothing.
    assert patient < staff < clinician


def test_the_narrower_views_are_strict_subsets(client_p1, token_for) -> None:
    """Scoping is a filter on one shared record, not four separate records.
    Every role's view must be a subset of the clinician's — if a role could see
    something the clinician cannot, the roles have diverged into different
    sources of truth, which is the fragmentation this product exists to fix."""
    clinician = _types_for(client_p1, token_for, "u-a-clinician", Role.CLINICIAN)
    for user_id, role in (
        ("u-a-staff", Role.STAFF),
        ("u-a-admin", Role.ADMIN),
        ("u-a-patient", Role.PATIENT),
    ):
        assert _types_for(client_p1, token_for, user_id, role) <= clinician


def test_ai_scribed_entries_are_structurally_distinct(client_p1, token_for) -> None:
    """The brief requires AI-scribed notes be distinguishable from manual ones.
    The flag is derived server-side from the type, so a client cannot disagree
    with the server about what is machine output."""
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    entries = client_p1.get("/patients/patient-a1/entries", headers=headers).json()

    ai = [e for e in entries if e["is_ai_scribed"]]
    manual = [e for e in entries if not e["is_ai_scribed"]]
    assert len(ai) == 1
    assert ai[0]["author_role"] == "system"
    assert all(e["author_role"] != "system" for e in manual)


# --------------------------------------------------------------------------
# Latency: an early, honest reading against the 300ms P95 target
# --------------------------------------------------------------------------


def test_timeline_read_is_comfortably_inside_the_latency_budget(client_p1, token_for) -> None:
    """The brief targets P95 <= 300ms for the Glance View on a warm path. There
    is no Glance View yet, so this measures its cheapest ancestor: a warm,
    in-process timeline read against SQLite, with no network and no browser.

    That makes the number a LOWER BOUND on real latency, not a measurement of
    it — recorded now so Phase 2 can see how much budget it is spending as
    highlights, comments and AI summaries land on this path. Threshold is set
    at 300ms rather than something tighter because a CI runner under load is
    noisy, and a flaky perf test gets deleted rather than fixed.
    """
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    url = "/patients/patient-a1/entries"

    client_p1.get(url, headers=headers)  # warm the path

    samples = []
    for _ in range(20):
        started = time.perf_counter()
        response = client_p1.get(url, headers=headers)
        samples.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200

    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    assert p95 < 300, f"P95 {p95:.1f}ms exceeds the 300ms budget"
