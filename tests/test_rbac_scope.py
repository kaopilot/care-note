"""Required test 1 of 4 — role and clinic access control, enforced server-side.

Named by the brief. Every request below goes at the HTTP API with a *valid*
token for the wrong role or the wrong clinic, never through the UI. That is the
point: a page that hides clinician sections from staff is worth nothing if the
API hands one over when asked by id, and the API is what an attacker talks to.

Four properties are asserted here:

1. **Roles cannot write as each other.** Staff cannot author or edit clinician
   sections; clinicians cannot author or edit staff notes. The brief requires
   both directions.
2. **Staff cannot read `clinician_sections`** — the least-privilege judgment
   call recorded as D-004, tested so that reversing it is a deliberate act that
   breaks a test rather than a silent drift.
3. **Patients cannot reach internal comments or raw AI-scribed notes.**
4. **No user can touch another clinic's data**, read or write.

Two refusal codes appear, and the difference is deliberate (D-022):

  * **403** — you exist, the thing exists, you may not have it.
  * **404** — cross-clinic. The response must not confirm that an id exists in
    another clinic, so the refusal is indistinguishable from "no such row".

Where a refusal could still leak, the assertion checks the *body* as well as
the status: a 403 that quotes the content it is refusing has refused nothing.
"""

from __future__ import annotations

import pytest

from app.core.enums import Role
from app.models import Comment, Entry


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------


@pytest.fixture()
def staff(token_for):
    return token_for("u-a-staff", Role.STAFF, "clinic-a")


@pytest.fixture()
def clinician(token_for):
    return token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")


@pytest.fixture()
def patient(token_for):
    return token_for("u-a-patient", Role.PATIENT, "clinic-a", patient_id="patient-a1")


@pytest.fixture()
def admin(token_for):
    return token_for("u-a-admin", Role.ADMIN, "clinic-a")


@pytest.fixture()
def clinician_b(token_for):
    """A legitimate clinician — of the *other* clinic."""
    return token_for("u-b-clinician", Role.CLINICIAN, "clinic-b")


@pytest.fixture()
def staff_b(token_for):
    return token_for("u-b-staff", Role.STAFF, "clinic-b")


# --------------------------------------------------------------------------
# 1. Staff and clinicians cannot write or edit as each other
# --------------------------------------------------------------------------


def test_staff_cannot_author_a_clinician_section(client_p1, staff):
    response = client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": "clinician_section", "content": "Impression: likely T2DM."},
        headers=staff,
    )
    assert response.status_code == 403


def test_clinician_cannot_author_a_staff_note(client_p1, clinician):
    """The converse, and the one a permissive build gets wrong.

    It is tempting to let the most privileged role write anything. But a
    `staff_note` is a record of who observed what, and a clinician writing one
    would put a nurse's name on an observation the nurse never made.
    """
    response = client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": "staff_note", "content": "BP 120/80 seated."},
        headers=clinician,
    )
    assert response.status_code == 403


def test_staff_cannot_edit_a_clinician_section(client_p1, staff):
    response = client_p1.patch(
        "/entries/entry-a1-clin",
        json={"content": "Staff rewriting the plan.", "expected_version": 1},
        headers=staff,
    )
    assert response.status_code == 403
    # The refusal must not hand over the content it is refusing.
    assert "HbA1c" not in response.text
    assert "microalbuminuria" not in response.text


def test_clinician_cannot_edit_a_staff_note(client_p1, clinician, seeded_p1):
    response = client_p1.patch(
        "/entries/entry-a1-staff",
        json={"content": "Clinician rewriting the observations.", "expected_version": 1},
        headers=clinician,
    )
    assert response.status_code == 403

    # And the refusal actually held at the database, not just at the response.
    entry = seeded_p1["db"].get(Entry, "entry-a1-staff")
    assert entry.content.startswith("BP 138/86")
    assert entry.version_number == 1


def test_neither_role_can_revert_the_others_notes(client_p1, staff, clinician):
    """Revert is a write. A role that cannot edit a note cannot rewind it either
    — otherwise the write matrix would have a back door with a different name."""
    assert client_p1.post(
        "/entries/entry-a1-clin/revert", json={"to_version": 1}, headers=staff
    ).status_code == 403
    assert client_p1.post(
        "/entries/entry-a1-staff/revert", json={"to_version": 1}, headers=clinician
    ).status_code == 403


def test_admin_can_read_everything_in_clinic_but_author_nothing(client_p1, admin):
    """D-011: admin is oversight, not authorship, so an admin account cannot
    quietly alter the clinical record."""
    timeline = client_p1.get("/patients/patient-a1/entries", headers=admin)
    assert timeline.status_code == 200
    assert {e["type"] for e in timeline.json()} >= {"clinician_section", "staff_note"}

    for entry_type in ("staff_note", "clinician_section", "patient_instruction"):
        refused = client_p1.post(
            "/patients/patient-a1/entries",
            json={"type": entry_type, "content": "admin wrote this"},
            headers=admin,
        )
        assert refused.status_code == 403, f"admin authored a {entry_type}"


# --------------------------------------------------------------------------
# 2. Staff cannot view clinician_sections (D-004)
# --------------------------------------------------------------------------


def test_staff_cannot_fetch_a_clinician_section_by_id(client_p1, staff):
    response = client_p1.get("/entries/entry-a1-clin", headers=staff)
    assert response.status_code == 403
    assert "HbA1c" not in response.text


def test_staff_timeline_omits_clinician_sections_entirely(client_p1, staff):
    """Absent from the listing, not merely refused on fetch.

    A build that returned the row and let the client hide it would leak the
    existence, timestamp and author of every clinician section.
    """
    entries = client_p1.get("/patients/patient-a1/entries", headers=staff).json()
    assert "clinician_section" not in {e["type"] for e in entries}
    assert "staff_note" in {e["type"] for e in entries}, "staff lost their own notes"


def test_staff_cannot_read_clinician_section_history_or_diffs(client_p1, staff):
    """Version history is a second copy of the content. Guarding the entry and
    leaving `/versions` open would make the refusal cosmetic."""
    assert client_p1.get(
        "/entries/entry-a1-clin/versions", headers=staff
    ).status_code == 403
    assert client_p1.get(
        "/entries/entry-a1-clin/diff?from_version=1&to_version=1", headers=staff
    ).status_code == 403


def test_staff_cannot_reach_a_clinician_section_through_provenance(client_p1, staff):
    """A pointer is a reference, never an authorisation.

    Provenance resolution is the natural side door: a valid pointer to content
    the role is refused head-on must not return that content.
    """
    response = client_p1.get(
        "/provenance", params={"pointer": "entry://entry-a1-clin"}, headers=staff
    )
    assert response.status_code == 403
    assert "HbA1c" not in response.text


# --------------------------------------------------------------------------
# 3. Patients cannot reach internal comments or raw AI-scribed notes
# --------------------------------------------------------------------------


def test_patient_cannot_read_internal_comments(client_p1, staff, patient):
    client_p1.post(
        "/entries/entry-a1-staff/comments",
        json={"body": "Chasing the lab about the microalbuminuria result."},
        headers=staff,
    )
    response = client_p1.get("/entries/entry-a1-staff/comments", headers=patient)
    assert response.status_code == 403
    assert "microalbuminuria" not in response.text


def test_patient_cannot_write_into_an_internal_thread(client_p1, patient):
    """D-035 goes beyond the brief: patients cannot write comments either.

    Letting someone post into a thread they cannot read the rest of is worse
    than not offering it. The patient's voice reaches the record through
    `patient_note` entries instead, which are first-class timeline content.
    """
    assert client_p1.post(
        "/entries/entry-a1-staff/comments",
        json={"body": "let me in"},
        headers=patient,
    ).status_code == 403


def test_patient_cannot_fetch_a_raw_ai_scribed_note(client_p1, patient):
    """The brief is explicit: patients see summaries and instructions, not the
    raw AI-scribed notes those are derived from."""
    assert client_p1.get("/entries/entry-a1-ai", headers=patient).status_code == 403


def test_patient_timeline_contains_only_patient_facing_types(client_p1, clinician, patient):
    """Run a real scribe first, so the assertion covers a freshly generated AI
    note and not only the seeded one."""
    client_p1.post(
        "/patients/patient-a1/scribe",
        json={"interaction_type": "doctor_patient_consult"},
        headers=clinician,
    )
    entries = client_p1.get("/patients/patient-a1/entries", headers=patient).json()
    assert {e["type"] for e in entries} <= {
        "patient_note",
        "patient_summary",
        "patient_instruction",
    }


def test_patient_cannot_open_the_clinical_glance_view_or_task_list(client_p1, patient):
    """The Glance View quotes internal content by design, so it is refused
    wholesale rather than filtered."""
    assert client_p1.get(
        "/patients/patient-a1/glance", headers=patient
    ).status_code == 403
    assert client_p1.get(
        "/patients/patient-a1/tasks", headers=patient
    ).status_code == 403


def test_patient_cannot_read_another_patient_in_the_same_clinic(client_p1, patient):
    """Same clinic, different person: 403 rather than 404, because the record
    does exist and pretending otherwise would lie to a legitimate user."""
    assert client_p1.get("/patients/patient-a2", headers=patient).status_code == 403
    assert client_p1.get(
        "/patients/patient-a2/entries", headers=patient
    ).status_code == 403


# --------------------------------------------------------------------------
# 4. Cross-clinic isolation — the other half of RBAC
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/patients/patient-a1", None),
        ("get", "/patients/patient-a1/entries", None),
        ("get", "/entries/entry-a1-clin", None),
        ("get", "/entries/entry-a1-clin/versions", None),
        ("get", "/entries/entry-a1-staff/comments", None),
        ("get", "/patients/patient-a1/highlights", None),
        ("get", "/patients/patient-a1/glance", None),
        ("get", "/patients/patient-a1/tasks", None),
    ],
)
def test_clinic_b_cannot_read_clinic_a_data(client_p1, clinician_b, method, path, body):
    """404 everywhere, not 403 — the response must not confirm the id exists."""
    response = getattr(client_p1, method)(path, headers=clinician_b, **({"json": body} if body else {}))
    assert response.status_code == 404, f"{method.upper()} {path} returned {response.status_code}"
    assert "HbA1c" not in response.text
    assert "Amira" not in response.text


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/patients/patient-a1/entries",
         {"type": "clinician_section", "content": "written from another clinic"}),
        ("patch", "/entries/entry-a1-clin",
         {"content": "written from another clinic", "expected_version": 1}),
        ("post", "/entries/entry-a1-clin/revert", {"to_version": 1}),
        ("post", "/entries/entry-a1-staff/comments", {"body": "from another clinic"}),
        ("post", "/patients/patient-a1/tasks", {"description": "from another clinic"}),
        ("post", "/entries/entry-a1-clin/highlights", {"span_start": 0, "span_end": 5}),
        ("post", "/patients/patient-a1/scribe",
         {"interaction_type": "doctor_patient_consult"}),
    ],
)
def test_clinic_b_cannot_write_to_clinic_a_data(
    client_p1, clinician_b, seeded_p1, method, path, body
):
    response = getattr(client_p1, method)(path, json=body, headers=clinician_b)
    assert response.status_code == 404, f"{method.upper()} {path} returned {response.status_code}"

    # Nothing landed. Asserted against the database, because a refusal that
    # returns 404 while still committing the row would pass a status check.
    db = seeded_p1["db"]
    assert db.get(Entry, "entry-a1-clin").content.startswith("T2DM")
    assert db.get(Entry, "entry-a1-clin").version_number == 1
    assert (
        db.query(Entry).filter(Entry.clinic_id == "clinic-b",
                               Entry.patient_id == "patient-a1").count() == 0
    )
    assert db.query(Comment).filter(Comment.entry_id == "entry-a1-staff").count() == 0


def test_staff_of_clinic_b_cannot_list_clinic_a_patients(client_p1, staff_b):
    """The list route is the one that leaks by omission rather than by refusal
    — it returns 200, so the assertion has to be on the contents."""
    patients = client_p1.get("/patients", headers=staff_b).json()
    assert {p["id"] for p in patients} == {"patient-b1", "patient-b2"}


def test_a_valid_pointer_does_not_cross_a_clinic_boundary(client_p1, clinician_b):
    """The pointer grammar is public and ids are guessable from a URL. If
    `resolve()` did not enforce clinic scope, provenance would become a
    cross-tenant read primitive."""
    response = client_p1.get(
        "/provenance", params={"pointer": "entry://entry-a1-clin"}, headers=clinician_b
    )
    assert response.status_code == 404
    assert "HbA1c" not in response.text


def test_clinic_id_is_taken_from_the_token_not_the_request(
    client_p1, staff_b, seeded_p1
):
    """The clinic claim cannot be widened by the client.

    A body field named `clinic_id` must be ignored, not honoured — otherwise
    every scoping guarantee above is one crafted request away from nothing.
    Asserted against the stored row, because `EntryOut` does not expose
    `clinic_id` at all: the wire format cannot confirm this, only the database
    can.
    """
    response = client_p1.post(
        "/patients/patient-b1/entries",
        json={
            "type": "staff_note",
            "content": "Attempting to plant a note in another clinic.",
            "clinic_id": "clinic-a",
        },
        headers=staff_b,
    )
    assert response.status_code == 201

    stored = seeded_p1["db"].get(Entry, response.json()["id"])
    assert stored.clinic_id == "clinic-b"
    assert stored.author_id == "u-b-staff"


# --------------------------------------------------------------------------
# 5. Stored payloads round-trip verbatim (D-015, both halves)
# --------------------------------------------------------------------------

# A script tag, and clinical prose that legitimately contains angle brackets.
# One string pins both halves of D-015 at once: the payload must not be
# executed, and the dose limits must not be silently altered.
XSS_AND_CLINICAL = (
    "<script>alert('xss')</script> Target BP <130/80, hold if dose <5mg or sats <92% on RA."
)


def test_script_payload_in_a_note_is_returned_as_the_literal_text_written(
    client_p1, staff
):
    """Neither executed nor silently altered.

    The build deliberately does NOT escape or strip on write (D-015): escaping
    would double-escape at the React render boundary, and tag-stripping can eat
    `<5mg` and turn a dose limit into `mg`. Silently altering a clinical note is
    a patient-safety bug and a worse one than the XSS it would be defending
    against — the XSS is already dead because nothing renders content as HTML
    (asserted structurally in test_sanitization.py).
    """
    created = client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": "staff_note", "content": XSS_AND_CLINICAL},
        headers=staff,
    )
    assert created.status_code == 201
    assert created.json()["content"] == XSS_AND_CLINICAL

    fetched = client_p1.get(f"/entries/{created.json()['id']}", headers=staff)
    assert fetched.json()["content"] == XSS_AND_CLINICAL
    # Specifically: not escaped, and the dose limits survived intact.
    assert "&lt;" not in fetched.text
    assert "dose <5mg" in fetched.json()["content"]


def test_script_payload_in_a_comment_is_returned_as_the_literal_text_written(
    client_p1, staff
):
    created = client_p1.post(
        "/entries/entry-a1-staff/comments",
        json={"body": XSS_AND_CLINICAL},
        headers=staff,
    )
    assert created.status_code == 201
    assert created.json()["body"] == XSS_AND_CLINICAL

    listed = client_p1.get("/entries/entry-a1-staff/comments", headers=staff).json()
    assert listed[0]["body"] == XSS_AND_CLINICAL


def test_the_payload_is_flagged_in_the_audit_trail_without_being_stored_there(
    client_p1, staff, seeded_p1
):
    """Detection without alteration: the attempt is visible to whoever reviews
    the logs, while the note itself is untouched and the log holds no prose."""
    from app.models import AuditLog

    created = client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": "staff_note", "content": XSS_AND_CLINICAL},
        headers=staff,
    ).json()

    row = (
        seeded_p1["db"]
        .query(AuditLog)
        .filter(AuditLog.target_id == created["id"])
        .one()
    )
    assert "script_tag" in row.audit_metadata
    assert "alert" not in row.audit_metadata
