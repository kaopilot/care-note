"""Regressions for four defects reported against the Phase 6 build.

Each one was reproducible against the seeded fixture and invisible to the 385
tests that already passed, which is the interesting part: every one of them
lives in the seam between two pieces of correct code.

  1. A hand-marked highlight vanished from the Glance View. `create_manual_
     highlight` added a +0.5 bonus; the `refresh_patient_highlights` call the
     same request makes immediately afterwards recomputed the score from
     `score_span` alone and erased it. Below the six-item cap, gone.

  2. Suggestion ids changed on every write, so the Glance View a clinician was
     looking at held ids the server had already deleted. Confirming a second
     suggestion returned 404.

  3. "New since your last visit" never populated during a first session:
     `previous_viewed_at` was left NULL, so there was nothing to compare
     against on the next load either.

  4. Timestamps left the API without a UTC offset, so the browser parsed them
     as local time and every relative age was wrong by the viewer's offset.

Numbered D-059 / D-060 / D-061 in DECISIONS.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.enums import HighlightStatus, Role
from app.models import Entry, Patient, PatientView
from app.services import glance as glance_service
from app.services import highlights as highlight_service


@pytest.fixture()
def clinician(token_for):
    return token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")


@pytest.fixture()
def staff(token_for):
    return token_for("u-a-staff", Role.STAFF, "clinic-a")


@pytest.fixture()
def with_suggestions(client_p1, clinician):
    """`seeded_p1` inserts entries directly, so no highlights exist yet.

    The refresh endpoint runs the same generation path an entry write would,
    which is what these tests need to have something to confirm.
    """
    client_p1.post("/patients/patient-a1/highlights/refresh", headers=clinician)
    return client_p1


# --------------------------------------------------------------------------
# 1 — a clinician's own highlight keeps its precedence
# --------------------------------------------------------------------------


def test_manual_highlight_keeps_its_bonus_through_the_refresh_it_triggers(
    client_p1, clinician
):
    """The bug in one assertion: create, then read back, and compare.

    The route itself calls `refresh_patient_highlights` after creating the
    highlight, so the response body was already the rescored value. Anything
    that rescores — an edit, a task, another accept — did it again.
    """
    created = client_p1.post(
        "/entries/entry-a1-clin/highlights",
        json={"span_start": 0, "span_end": 45},
        headers=clinician,
    )
    assert created.status_code == 201
    body = created.json()

    assert body["is_manual"] is True
    assert body["score_breakdown"].get("manual") == pytest.approx(
        highlight_service.MANUAL_HIGHLIGHT_BONUS
    ), "the manual term was dropped from the breakdown by the rescore"
    assert body["score"] >= highlight_service.MANUAL_HIGHLIGHT_BONUS

    # Now force another rescore and confirm the bonus survives it too.
    client_p1.post(
        "/patients/patient-a1/highlights/refresh", headers=clinician
    )
    after = {
        row["id"]: row
        for row in client_p1.get(
            "/patients/patient-a1/highlights", headers=clinician
        ).json()
    }[body["id"]]
    assert after["score_breakdown"].get("manual") == pytest.approx(
        highlight_service.MANUAL_HIGHLIGHT_BONUS
    )
    assert after["score"] == pytest.approx(body["score"], abs=0.05)


def test_manual_highlight_outranks_machine_suggestions_on_the_card(
    with_suggestions, clinician
):
    """The symptom, rather than the mechanism: it has to be *on* the card.

    `_top_highlights` caps at MAX_HIGHLIGHTS and orders accepted-then-score, so
    a manual highlight scored without its bonus sank below six confirmed
    suggestions and fell off the bottom.
    """
    # Confirm as many suggestions as the card offers, re-reading each time —
    # exactly what the UI does.
    for _ in range(glance_service.MAX_HIGHLIGHTS):
        card = with_suggestions.get("/patients/patient-a1/glance", headers=clinician).json()
        pending = [h for h in card["highlights"] if h["status"] == "suggested"]
        if not pending:
            break
        with_suggestions.post(f"/highlights/{pending[0]['id']}/accept", headers=clinician)

    marked = with_suggestions.post(
        "/entries/entry-a1-ai/highlights",
        json={"span_start": 0, "span_end": 40},
        headers=clinician,
    )
    assert marked.status_code == 201
    highlight_id = marked.json()["id"]

    card = with_suggestions.get("/patients/patient-a1/glance", headers=clinician).json()
    shown = [h["id"] for h in card["highlights"]]
    assert highlight_id in shown, "the clinician's own mark is missing from the card"
    assert card["highlights"][0]["id"] == highlight_id, (
        "a hand-marked span should lead the card, not merely appear on it"
    )


# --------------------------------------------------------------------------
# 2 — suggestion ids survive a refresh
# --------------------------------------------------------------------------


def test_confirming_one_suggestion_does_not_invalidate_the_others(
    with_suggestions, clinician
):
    """Accept every suggestion the card showed, using the ids it showed.

    Before the fix only the first succeeded: accepting it regenerated the rest
    with new uuids, and the remaining five 404ed.
    """
    card = with_suggestions.get("/patients/patient-a1/glance", headers=clinician).json()
    suggested = [h["id"] for h in card["highlights"] if h["status"] == "suggested"]
    assert len(suggested) >= 3, "fixture should offer several suggestions"

    for highlight_id in suggested:
        response = with_suggestions.post(f"/highlights/{highlight_id}/accept", headers=clinician)
        assert response.status_code == 200, (
            f"{highlight_id} was regenerated out from under the open card"
        )


def test_unrelated_writes_do_not_renumber_open_suggestions(with_suggestions, clinician):
    """A task, a comment or an edit elsewhere must not break the card in hand."""
    card = with_suggestions.get("/patients/patient-a1/glance", headers=clinician).json()
    before = {h["id"] for h in card["highlights"] if h["status"] == "suggested"}

    with_suggestions.post(
        "/patients/patient-a1/tasks",
        json={"description": "book monofilament testing", "entry_id": "entry-a1-clin"},
        headers=clinician,
    )
    with_suggestions.patch(
        "/entries/entry-a1-clin",
        json={
            "content": "T2DM, HbA1c 8.4%. Repeat ACR. Keep BP <130/80.",
            "expected_version": 1,
            "change_summary": "edited in consult",
        },
        headers=clinician,
    )

    after = {
        row["id"]
        for row in with_suggestions.get(
            "/patients/patient-a1/highlights", headers=clinician
        ).json()
        if row["status"] == "suggested"
    }

    # Spans on the edited entry may legitimately change — the words moved. Every
    # other suggestion the clinician had in front of them must still resolve.
    untouched = {
        h["id"]
        for h in card["highlights"]
        if h["status"] == "suggested" and h["entry_id"] != "entry-a1-clin"
    }
    assert untouched, "fixture should offer suggestions outside the edited entry"
    assert untouched <= after, (
        "suggestions on entries nobody touched were recycled by an unrelated write"
    )
    assert before & after, "every suggestion id was recycled"


def test_a_span_that_stops_being_a_candidate_is_still_removed(with_suggestions, clinician):
    """The upsert must not turn into an append — D-055's original bug."""
    counts = []
    for _ in range(3):
        with_suggestions.post("/patients/patient-a1/highlights/refresh", headers=clinician)
        rows = with_suggestions.get(
            "/patients/patient-a1/highlights", headers=clinician
        ).json()
        counts.append(len(rows))
    assert counts[0] == counts[1] == counts[2], (
        f"repeated refreshes changed the highlight count: {counts}"
    )


# --------------------------------------------------------------------------
# 3 — "new since your last visit"
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "user_id,role,entry_type,content",
    [
        ("u-a-clinician", Role.CLINICIAN, "clinician_section", "Plan updated today."),
        ("u-a-staff", Role.STAFF, "staff_note", "BP 142/88 seated this morning."),
    ],
)
def test_a_note_written_during_the_first_session_shows_as_new_on_reload(
    client_p1, token_for, user_id, role, entry_type, content
):
    """Open a chart, write something, reload. It has to be there.

    Previously `previous_viewed_at` was left NULL on the first view, so the
    reload had nothing to compare against and reported `first_visit` with zero
    new entries — for every clinical role.
    """
    headers = token_for(user_id, role, "clinic-a")

    first = client_p1.get("/patients/patient-a1/glance", headers=headers).json()
    assert first["whats_new"]["first_visit"] is True

    client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": entry_type, "content": content},
        headers=headers,
    )

    reloaded = client_p1.get("/patients/patient-a1/glance", headers=headers).json()
    whats_new = reloaded["whats_new"]
    assert whats_new["first_visit"] is False
    assert whats_new["since"] is not None
    assert whats_new["count"] >= 1, "the note written this session was not surfaced"


def test_the_marker_holds_still_across_a_rapid_refresh(client_p1, clinician):
    """D-033's guarantee, unchanged: reading the news must not destroy it."""
    client_p1.get("/patients/patient-a1/glance", headers=clinician)
    client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": "clinician_section", "content": "Titration reviewed."},
        headers=clinician,
    )
    first = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    second = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()

    assert first["whats_new"]["since"] == second["whats_new"]["since"]
    assert second["whats_new"]["count"] == first["whats_new"]["count"]


def test_the_marker_rolls_forward_once_it_is_older_than_the_cap(seeded_p1):
    """A chart left open all shift must not keep widening its own window.

    The marker only advanced on a >VIEW_SESSION_GAP gap between consecutive
    loads, so a user refreshing more often than that never got a fresh one and
    "since your last visit" quietly became "since this morning".
    """
    db = seeded_p1["db"]
    patient = db.query(Patient).filter(Patient.id == "patient-a1").one()

    glance_service.touch_view(db, user_id="u-a-clinician", patient=patient)
    row = (
        db.query(PatientView)
        .filter(
            PatientView.user_id == "u-a-clinician",
            PatientView.patient_id == "patient-a1",
        )
        .one()
    )

    # Age the whole session past the cap without opening a gap between loads.
    stale = datetime.now(timezone.utc) - glance_service.MAX_MARKER_AGE - timedelta(minutes=5)
    row.previous_viewed_at = stale
    row.last_viewed_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()

    since = glance_service.touch_view(db, user_id="u-a-clinician", patient=patient)
    assert since is not None
    assert since > stale, "a marker older than the cap should have rolled forward"


# --------------------------------------------------------------------------
# 4 — every timestamp on the wire carries an offset
# --------------------------------------------------------------------------


def _assert_utc(label: str, value) -> None:
    assert isinstance(value, str) and value, f"{label} is not a timestamp string"
    assert value.endswith("Z") or value[-6] in "+-", (
        f"{label} = {value!r} has no UTC offset; a browser will read it as "
        f"local time and every relative age will be wrong by the viewer's offset"
    )


def test_glance_timestamps_are_all_offset_qualified(client_p1, clinician):
    client_p1.get("/patients/patient-a1/glance", headers=clinician)
    client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": "clinician_section", "content": "Reviewed at the desk."},
        headers=clinician,
    )
    card = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()

    _assert_utc("generated_at", card["generated_at"])
    _assert_utc("whats_new.since", card["whats_new"]["since"])
    for entry in card["whats_new"]["entries"]:
        _assert_utc("whats_new.entries[].timestamp", entry["timestamp"])
    for highlight in card["highlights"]:
        _assert_utc("highlights[].entry_timestamp", highlight["entry_timestamp"])
    for flag in card["risk_flags"] + card["conflicts"]:
        _assert_utc("flag.timestamp", flag["timestamp"])
    for action in card["open_actions"]:
        _assert_utc("open_actions[].created_at", action["created_at"])


def test_entry_and_version_timestamps_are_offset_qualified(client_p1, clinician):
    entries = client_p1.get("/patients/patient-a1/entries", headers=clinician).json()
    for entry in entries:
        _assert_utc("entry.timestamp", entry["timestamp"])

    versions = client_p1.get(
        "/entries/entry-a1-clin/versions", headers=clinician
    ).json()
    for version in versions:
        _assert_utc("version.edited_at", version["edited_at"])


def test_comment_and_task_timestamps_are_offset_qualified(client_p1, clinician):
    client_p1.post(
        "/entries/entry-a1-clin/comments",
        json={"body": "Please chase the ACR result.", "mentions": []},
        headers=clinician,
    )
    client_p1.post(
        "/patients/patient-a1/tasks",
        json={"description": "chase ACR", "entry_id": "entry-a1-clin"},
        headers=clinician,
    )

    for comment in client_p1.get(
        "/entries/entry-a1-clin/comments", headers=clinician
    ).json():
        _assert_utc("comment.created_at", comment["created_at"])
    for task in client_p1.get("/patients/patient-a1/tasks", headers=clinician).json():
        _assert_utc("task.created_at", task["created_at"])


def test_patient_view_timestamps_are_offset_qualified(client_p1, token_for):
    headers = token_for(
        "u-a-patient", Role.PATIENT, "clinic-a", patient_id="patient-a1"
    )
    care = client_p1.get("/patients/patient-a1/my-care", headers=headers).json()
    _assert_utc("generated_at", care["generated_at"])
    for update in care["updates"] + care["your_notes"]:
        _assert_utc("written_at", update["written_at"])


# --------------------------------------------------------------------------
# 5 — a task can actually be closed
# --------------------------------------------------------------------------


def test_closing_a_task_removes_it_from_open_actions(client_p1, clinician):
    """The endpoint always worked; nothing in the UI called it.

    Pinned here because an open action that cannot be closed also inflates
    `action_score` forever, which quietly distorts the ranking on the card.
    """
    created = client_p1.post(
        "/patients/patient-a1/tasks",
        json={"description": "book monofilament testing", "entry_id": "entry-a1-clin"},
        headers=clinician,
    )
    task_id = created.json()["id"]

    card = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    assert task_id in [a["id"] for a in card["open_actions"] if a["kind"] == "task"]
    assert card["counts"]["open_tasks"] >= 1

    done = client_p1.post(
        f"/tasks/{task_id}/status", json={"status": "done"}, headers=clinician
    )
    assert done.status_code == 200
    assert done.json()["closed_at"] is not None

    card = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    assert task_id not in [a["id"] for a in card["open_actions"] if a["kind"] == "task"]


def test_staff_can_close_a_task_assigned_to_them(client_p1, clinician, staff):
    """Whoever the work landed on has to be able to tick it off."""
    created = client_p1.post(
        "/patients/patient-a1/tasks",
        json={
            "description": "repeat BP at 10 minutes",
            "entry_id": "entry-a1-staff",
            "assigned_to": "u-a-staff",
        },
        headers=clinician,
    )
    task_id = created.json()["id"]

    response = client_p1.post(
        f"/tasks/{task_id}/status", json={"status": "done"}, headers=staff
    )
    assert response.status_code == 200
    assert response.json()["status"] == "done"
