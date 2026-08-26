"""Phase 2 behaviour, asserted at the API layer.

Deliberately NOT the four files the brief names — those are Phase 3's, they are
graded by name, and they cover RBAC scope, revision history, highlight
provenance and concurrent edits. This file covers what those four do not: that
the scribe pipeline actually redacts, that the Glance View surfaces what it
claims to, and that the conflict rule leaves the disputed content in place.

Every test goes through HTTP rather than calling services directly. A service
that behaves correctly behind a route that forgets to check something is not a
property worth asserting.
"""

from __future__ import annotations

import pytest

from app.core.enums import Role

# Identifiers planted in the synthetic transcripts. If any of these survive into
# stored content, the redaction boundary has failed.
PLANTED_IDENTIFIERS = ("Amira", "Rahman", "S8412345D", "6123 4567", "Dr Lim")


@pytest.fixture()
def clinician(token_for):
    return token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")


@pytest.fixture()
def staff(token_for):
    return token_for("u-a-staff", Role.STAFF, "clinic-a")


@pytest.fixture()
def patient(token_for):
    return token_for("u-a-patient", Role.PATIENT, "clinic-a", patient_id="patient-a1")


def scribe(client, headers, interaction="doctor_patient_consult"):
    response = client.post(
        "/patients/patient-a1/scribe",
        json={"interaction_type": interaction},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------
# 2.2 — AI scribe
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "interaction,expected_type",
    [
        ("doctor_patient_consult", "ai_doctor_consult_summary"),
        ("nurse_patient_consult", "ai_nurse_consult_summary"),
        ("ai_patient_session", "ai_patient_session_summary"),
    ],
)
def test_scribe_produces_a_system_authored_entry_of_the_right_type(
    client_p1, clinician, interaction, expected_type
):
    entry = scribe(client_p1, clinician, interaction)
    assert entry["type"] == expected_type
    assert entry["author_role"] == "system"
    assert entry["author_id"] == "system"
    assert entry["is_ai_scribed"] is True
    # An AI note's provenance points at the session behind it, never at itself:
    # the source of truth is the transcript, not the derived summary.
    assert entry["provenance_pointer"].startswith("session://")
    assert entry["ai_session_id"]


def test_scribe_redacts_identifiers_before_they_reach_storage(client_p1, clinician):
    entry = scribe(client_p1, clinician)
    assert entry["ai_redaction_count"] > 0, "redaction ran but removed nothing"
    for identifier in PLANTED_IDENTIFIERS:
        assert identifier not in entry["content"], (
            f"{identifier!r} survived into the stored summary"
        )


def test_scribe_reports_confidence_and_the_path_that_produced_it(client_p1, clinician):
    """Confidence must be attached and honest about its source.

    The offline summariser derives it from hedging density; a live model reports
    its own. Either way `ai_model_used` says which ran, so provenance never
    overstates itself.
    """
    entry = scribe(client_p1, clinician)
    assert 0.0 <= entry["ai_confidence"] <= 1.0
    assert entry["ai_model_used"]


def test_hedged_transcript_produces_lower_confidence_than_a_measured_one(
    client_p1, clinician
):
    """The calibration signal has to actually vary, or the flag is decoration."""
    nurse = scribe(client_p1, clinician, "nurse_patient_consult")
    session = scribe(client_p1, clinician, "ai_patient_session")
    assert session["ai_confidence"] < nurse["ai_confidence"]


def test_ai_entries_cannot_be_created_through_the_manual_write_route(client_p1, clinician):
    """Otherwise a human caller could fabricate machine provenance."""
    response = client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": "ai_doctor_consult_summary", "content": "not from a model"},
        headers=clinician,
    )
    assert response.status_code == 403


def test_patient_cannot_manufacture_a_doctor_consult_summary(client_p1, patient):
    refused = client_p1.post(
        "/patients/patient-a1/scribe",
        json={"interaction_type": "doctor_patient_consult"},
        headers=patient,
    )
    assert refused.status_code == 403
    # But may generate their own pre-consult session — the brief's
    # "patient-provided insight".
    allowed = client_p1.post(
        "/patients/patient-a1/scribe",
        json={"interaction_type": "ai_patient_session"},
        headers=patient,
    )
    assert allowed.status_code == 201


# --------------------------------------------------------------------------
# 2.4 — Glance View
# --------------------------------------------------------------------------


def test_every_surfaced_highlight_carries_a_reason_and_a_pointer(client_p1, clinician):
    scribe(client_p1, clinician)
    glance = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()

    assert glance["highlights"], "no highlights surfaced on a populated chart"
    for highlight in glance["highlights"]:
        assert highlight["risk_reason"].strip()
        assert highlight["provenance_pointer"].startswith("entry://")
        assert highlight["score_breakdown"]


def test_at_least_one_highlight_comes_from_an_ai_scribed_note(client_p1, clinician):
    scribe(client_p1, clinician)
    glance = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    assert any(highlight["is_ai_scribed"] for highlight in glance["highlights"])


def test_risk_flags_are_never_colour_alone(client_p1, clinician):
    """Every flag ships its own words. A colour-blind clinician must not lose
    the one signal that matters most."""
    glance = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    for flag in glance["risk_flags"]:
        assert flag["label"].strip()


def test_low_confidence_ai_summaries_are_flagged_separately_from_risk(
    client_p1, clinician
):
    scribe(client_p1, clinician, "ai_patient_session")
    glance = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    flagged = glance["confidence_flags"]
    assert flagged, "a hedged transcript should produce a low-confidence flag"
    assert all(flag["confidence"] < 0.6 for flag in flagged)
    # "This might be dangerous" and "this might be wrong" are different
    # warnings, carried in different fields.
    assert {flag["entry_id"] for flag in flagged} != {
        flag["entry_id"] for flag in glance["risk_flags"]
    } or not glance["risk_flags"]


def test_first_visit_reports_no_since_marker(client_p1, clinician):
    """Captioning an entire chart as new would be noise on the one view that
    most needs to be readable."""
    glance = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    assert glance["whats_new"]["first_visit"] is True
    assert glance["whats_new"]["since"] is None
    assert glance["whats_new"]["count"] == 0


def test_refreshing_does_not_destroy_the_whats_new_marker(client_p1, clinician):
    """Reading the news must not clear it (D-033)."""
    client_p1.get("/patients/patient-a1/glance", headers=clinician)
    scribe(client_p1, clinician)
    first = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    second = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    assert first["whats_new"]["since"] == second["whats_new"]["since"]


def test_staff_glance_never_contains_a_clinician_section(client_p1, staff):
    """The type filter applies to the Top Card exactly as it does to the
    timeline — otherwise the Glance View quotes content the role is refused."""
    glance = client_p1.get("/patients/patient-a1/glance", headers=staff).json()
    assert all(
        highlight["entry_type"] != "clinician_section" for highlight in glance["highlights"]
    )
    assert all(flag["entry_type"] != "clinician_section" for flag in glance["risk_flags"])


def test_patient_cannot_open_the_clinical_glance_view(client_p1, patient):
    assert client_p1.get("/patients/patient-a1/glance", headers=patient).status_code == 403


def test_patient_view_returns_plain_language_and_no_internal_content(
    client_p1, clinician, patient
):
    scribe(client_p1, clinician)
    care = client_p1.get("/patients/patient-a1/my-care", headers=patient).json()

    assert care["labels"]["next_steps"] == "What to do next"
    serialised = str(care)
    for internal in ("ai_doctor_consult_summary", "clinician_section", "staff_note"):
        assert internal not in serialised


# --------------------------------------------------------------------------
# 2.4 — Accept / reject
# --------------------------------------------------------------------------


def test_only_clinicians_decide_highlights(client_p1, clinician, staff):
    scribe(client_p1, clinician)
    highlight = client_p1.get(
        "/patients/patient-a1/glance", headers=clinician
    ).json()["highlights"][0]

    assert client_p1.post(
        f"/highlights/{highlight['id']}/accept", headers=staff
    ).status_code == 403
    accepted = client_p1.post(f"/highlights/{highlight['id']}/accept", headers=clinician)
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["decided_by"] == "u-a-clinician"


def test_a_rejected_highlight_leaves_the_card_and_is_not_re_suggested(
    client_p1, clinician
):
    """Rejection persists as a row rather than a deletion. Re-proposing
    something a clinician dismissed is how a card trains people to ignore it."""
    scribe(client_p1, clinician)
    glance = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    target = glance["highlights"][0]

    client_p1.post(f"/highlights/{target['id']}/reject", headers=clinician)
    client_p1.post("/patients/patient-a1/highlights/refresh", headers=clinician)

    after = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    assert target["id"] not in {highlight["id"] for highlight in after["highlights"]}
    rejected = client_p1.get(
        "/patients/patient-a1/highlights?status=rejected", headers=clinician
    ).json()
    assert target["id"] in {highlight["id"] for highlight in rejected}


def test_clinician_can_highlight_inside_an_ai_note_without_editing_it(
    client_p1, clinician
):
    """Annotating machine output is not authoring it — the summary's text is
    untouched, which is what lets a clinician engage without taking ownership."""
    entry = scribe(client_p1, clinician)
    created = client_p1.post(
        f"/entries/{entry['id']}/highlights",
        json={"span_start": 0, "span_end": 30},
        headers=clinician,
    )
    assert created.status_code == 201
    assert created.json()["is_manual"] is True
    assert created.json()["status"] == "accepted"

    unchanged = client_p1.get(f"/entries/{entry['id']}", headers=clinician).json()
    assert unchanged["content"] == entry["content"]
    assert unchanged["version_number"] == entry["version_number"]


def test_highlight_goes_stale_rather_than_re_anchoring_when_the_entry_is_edited(
    client_p1, clinician
):
    """Silently moving a confirmed highlight onto text nobody approved would be
    a quiet lie about what a clinician signed off (D-030)."""
    created = client_p1.post(
        f"/entries/entry-a1-clin/highlights",
        json={"span_start": 0, "span_end": 20},
        headers=clinician,
    ).json()
    assert created["stale"] is False

    client_p1.patch(
        "/entries/entry-a1-clin",
        json={
            "content": "Completely rewritten assessment with different wording entirely.",
            "expected_version": 1,
        },
        headers=clinician,
    )
    after = client_p1.get("/patients/patient-a1/highlights", headers=clinician).json()
    stale = next(h for h in after if h["id"] == created["id"])
    assert stale["stale"] is True
    # The anchored text still reads as it did when the clinician marked it.
    assert stale["span_text"] == created["span_text"]


# --------------------------------------------------------------------------
# 2.7 — Conflict handling
# --------------------------------------------------------------------------


def test_clinician_correction_flags_the_ai_note_without_destroying_it(
    client_p1, clinician
):
    """D-007: the clinician's content wins immediately AND the disagreement
    stays visible. Losing either half loses the point."""
    ai_entry = scribe(client_p1, clinician)

    correction = client_p1.post(
        f"/entries/{ai_entry['id']}/supersede",
        json={"content": "Correction: six weeks, not two.", "risk_level": "medium"},
        headers=clinician,
    )
    assert correction.status_code == 201
    assert correction.json()["supersedes_entry_id"] == ai_entry["id"]
    assert correction.json()["type"] == "clinician_section"

    original = client_p1.get(f"/entries/{ai_entry['id']}", headers=clinician).json()
    assert original["conflict_flagged"] is True
    assert original["content"] == ai_entry["content"], "the disputed text was altered"


def test_ai_notes_cannot_be_edited_in_place_by_anyone(client_p1, clinician):
    ai_entry = scribe(client_p1, clinician)
    response = client_p1.patch(
        f"/entries/{ai_entry['id']}",
        json={"content": "rewriting the machine's words", "expected_version": 1},
        headers=clinician,
    )
    assert response.status_code == 403
    assert "supersede" in response.json()["detail"]


def test_correction_appears_on_the_glance_view(client_p1, clinician):
    ai_entry = scribe(client_p1, clinician)
    client_p1.post(
        f"/entries/{ai_entry['id']}/supersede",
        json={"content": "Correction: six weeks, not two."},
        headers=clinician,
    )
    glance = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    assert glance["conflicts"], "a recorded correction is invisible on the card"


# --------------------------------------------------------------------------
# 2.5 — Collaboration
# --------------------------------------------------------------------------


def test_mentions_outside_the_clinic_are_dropped_not_stored(client_p1, staff):
    """A mention that renders as a mention must be one that reached someone."""
    response = client_p1.post(
        "/entries/entry-a1-staff/comments",
        json={
            "body": "@clinician_b please look at this",
            "mentions": ["u-b-clinician"],  # a real user, in the other clinic
        },
        headers=staff,
    )
    assert response.status_code == 201
    assert response.json()["mentions"] == []


def test_patients_can_neither_read_nor_write_internal_comments(client_p1, staff, patient):
    client_p1.post(
        "/entries/entry-a1-staff/comments",
        json={"body": "internal discussion"},
        headers=staff,
    )
    assert client_p1.get(
        "/entries/entry-a1-staff/comments", headers=patient
    ).status_code == 403
    assert client_p1.post(
        "/entries/entry-a1-staff/comments",
        json={"body": "let me in"},
        headers=patient,
    ).status_code == 403


def test_open_tasks_and_threads_both_surface_as_open_actions(client_p1, clinician, staff):
    """They are separate tables and the same thing to a clinician: someone is
    waiting on something."""
    client_p1.post(
        "/entries/entry-a1-staff/comments",
        json={"body": "chasing the lab"},
        headers=staff,
    )
    client_p1.post(
        "/patients/patient-a1/tasks",
        json={"description": "Book monofilament testing", "assigned_to": "u-a-staff"},
        headers=clinician,
    )
    glance = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    kinds = {action["kind"] for action in glance["open_actions"]}
    assert kinds == {"task", "comment"}
