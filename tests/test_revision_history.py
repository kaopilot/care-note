"""Required test 2 of 4 — versions, revert, and the audit trail.

Named by the brief. The property under test is that **the record only ever
grows**. An edit appends. A revert appends. Nothing in this system rewrites or
removes what the chart used to say, because a longitudinal record that can
silently lose its own history is not evidence of anything — and "revert" is the
single most tempting place to implement that loss by accident.

Three assertions the brief asks for, plus the ones that make them mean
something:

* editing increments the version number;
* reverting returns the content to a prior state;
* the audit log records who changed what — **metadata only**. That last
  constraint is tested as hard as the others: an audit trail that quotes note
  bodies has turned the log into a second, unguarded copy of the record, and it
  is the copy nobody applies RBAC to.
"""

from __future__ import annotations

import json

import pytest

from app.core.enums import Role
from app.models import AuditLog, Version

V1 = "Ankle sprain, neurovascularly intact. Advised RICE and review in 10 days."
V2 = "Ankle sprain, neurovascularly intact. Advised RICE and review in 5 days."
V3 = "Ankle sprain. Weight-bearing improved. Review in 5 days, no imaging needed."


@pytest.fixture()
def staff(token_for):
    return token_for("u-a-staff", Role.STAFF, "clinic-a")


@pytest.fixture()
def clinician(token_for):
    return token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")


@pytest.fixture()
def note(client_p1, staff):
    """A staff note at version 1, authored through the API like any other."""
    response = client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": "staff_note", "content": V1},
        headers=staff,
    )
    assert response.status_code == 201
    return response.json()


def _edit(client, note_id, headers, content, expected_version, summary=None):
    body = {"content": content, "expected_version": expected_version}
    if summary:
        body["change_summary"] = summary
    return client.patch(f"/entries/{note_id}", json=body, headers=headers)


# --------------------------------------------------------------------------
# Editing increments the version
# --------------------------------------------------------------------------


def test_a_new_entry_starts_at_version_one_with_a_version_row(client_p1, staff, note):
    """Every Entry has a Version from the moment it exists.

    Without this, revision history would have to back-fill an origin later, and
    the first edit would have nothing to diff against.
    """
    assert note["version_number"] == 1

    versions = client_p1.get(f"/entries/{note['id']}/versions", headers=staff).json()
    assert len(versions) == 1
    assert versions[0]["version_number"] == 1
    assert versions[0]["content"] == V1
    assert versions[0]["change_summary"] == "created"


def test_each_edit_increments_the_version_number(client_p1, staff, note):
    second = _edit(client_p1, note["id"], staff, V2, 1, "shortened review interval")
    assert second.status_code == 200
    assert second.json()["version_number"] == 2

    third = _edit(client_p1, note["id"], staff, V3, 2)
    assert third.status_code == 200
    assert third.json()["version_number"] == 3


def test_every_version_is_retained_with_its_own_snapshot(client_p1, staff, note):
    """Full snapshots rather than diffs (D-006): revert becomes a copy instead
    of a replay of an inverse patch chain, which is far harder to get subtly
    wrong — and revert correctness is what this file exists to prove."""
    _edit(client_p1, note["id"], staff, V2, 1)
    _edit(client_p1, note["id"], staff, V3, 2)

    versions = client_p1.get(f"/entries/{note['id']}/versions", headers=staff).json()
    assert [v["version_number"] for v in versions] == [3, 2, 1]  # newest first
    assert {v["content"] for v in versions} == {V1, V2, V3}


def test_a_version_records_who_made_it_and_in_what_role(client_p1, staff, note):
    _edit(client_p1, note["id"], staff, V2, 1, "shortened review interval")
    latest = client_p1.get(f"/entries/{note['id']}/versions", headers=staff).json()[0]

    assert latest["edited_by"] == "u-a-staff"
    assert latest["edited_by_role"] == "staff"
    assert latest["edited_by_name"] == "Nurse Priya"
    assert latest["change_summary"] == "shortened review interval"


def test_view_changes_since_x_reports_the_actual_difference(client_p1, staff, note):
    """The brief's "view changes since X". Returned as structured operations
    rather than rendered markup, so a note containing angle brackets diffs like
    any other line."""
    _edit(client_p1, note["id"], staff, V2, 1)

    diff = client_p1.get(
        f"/entries/{note['id']}/diff?from_version=1&to_version=2", headers=staff
    ).json()
    assert diff["added"] == 1 and diff["removed"] == 1

    ops = {line["op"] for line in diff["lines"]}
    assert ops <= {"equal", "insert", "delete"}
    assert any(line["op"] == "delete" and "10 days" in line["text"] for line in diff["lines"])
    assert any(line["op"] == "insert" and "5 days" in line["text"] for line in diff["lines"])


# --------------------------------------------------------------------------
# Reverting restores prior content — without destroying history
# --------------------------------------------------------------------------


def test_revert_returns_content_to_the_prior_state(client_p1, staff, note):
    """The headline assertion the brief names."""
    _edit(client_p1, note["id"], staff, V2, 1)
    _edit(client_p1, note["id"], staff, V3, 2)

    reverted = client_p1.post(
        f"/entries/{note['id']}/revert", json={"to_version": 1}, headers=staff
    )
    assert reverted.status_code == 200
    assert reverted.json()["content"] == V1

    # And it is the stored state, not just the response body.
    assert client_p1.get(f"/entries/{note['id']}", headers=staff).json()["content"] == V1


def test_revert_moves_the_version_forward_never_backward(client_p1, staff, note):
    """Reverting to v1 from v3 produces v4, not v1.

    Rolling the number backwards would erase the record of the edit being
    undone — which is the one thing an audit trail exists to prevent.
    """
    _edit(client_p1, note["id"], staff, V2, 1)
    _edit(client_p1, note["id"], staff, V3, 2)

    reverted = client_p1.post(
        f"/entries/{note['id']}/revert", json={"to_version": 1}, headers=staff
    ).json()
    assert reverted["version_number"] == 4

    versions = client_p1.get(f"/entries/{note['id']}/versions", headers=staff).json()
    assert [v["version_number"] for v in versions] == [4, 3, 2, 1]
    assert versions[0]["reverted_from_version"] == 1
    assert versions[0]["content"] == V1


def test_the_versions_that_were_reverted_away_from_survive(client_p1, staff, note):
    """The content someone reverted *away* from is often the clinically
    interesting part — it is what a reviewer asks about later."""
    _edit(client_p1, note["id"], staff, V2, 1)
    _edit(client_p1, note["id"], staff, V3, 2)
    client_p1.post(f"/entries/{note['id']}/revert", json={"to_version": 1}, headers=staff)

    contents = {
        v["content"]
        for v in client_p1.get(f"/entries/{note['id']}/versions", headers=staff).json()
    }
    assert V2 in contents and V3 in contents


def test_a_revert_is_itself_revertible(client_p1, staff, note):
    """Undo has to be undoable, or the first mistaken revert is permanent."""
    _edit(client_p1, note["id"], staff, V2, 1)
    client_p1.post(f"/entries/{note['id']}/revert", json={"to_version": 1}, headers=staff)

    back = client_p1.post(
        f"/entries/{note['id']}/revert", json={"to_version": 2}, headers=staff
    )
    assert back.status_code == 200
    assert back.json()["content"] == V2
    assert back.json()["version_number"] == 4


def test_reverting_to_the_current_version_is_refused(client_p1, staff, note):
    """A no-op that still appended a version would pad the history with
    entries that record nothing having happened."""
    response = client_p1.post(
        f"/entries/{note['id']}/revert", json={"to_version": 1}, headers=staff
    )
    assert response.status_code == 409


def test_reverting_to_a_version_that_does_not_exist_is_refused(client_p1, staff, note):
    response = client_p1.post(
        f"/entries/{note['id']}/revert", json={"to_version": 99}, headers=staff
    )
    assert response.status_code == 404


def test_version_rows_are_never_deleted_by_any_of_this(client_p1, staff, note, seeded_p1):
    """Asserted at the table, not through the API. A route that filtered
    deleted rows out of its response would pass every assertion above while
    still having destroyed history."""
    _edit(client_p1, note["id"], staff, V2, 1)
    _edit(client_p1, note["id"], staff, V3, 2)
    client_p1.post(f"/entries/{note['id']}/revert", json={"to_version": 1}, headers=staff)

    rows = (
        seeded_p1["db"]
        .query(Version)
        .filter(Version.entry_id == note["id"])
        .order_by(Version.version_number)
        .all()
    )
    assert [r.version_number for r in rows] == [1, 2, 3, 4]
    assert [r.content_snapshot for r in rows] == [V1, V2, V3, V1]


# --------------------------------------------------------------------------
# The audit log records who changed what — metadata only
# --------------------------------------------------------------------------


def _audit_rows(db, target_id):
    return (
        db.query(AuditLog)
        .filter(AuditLog.target_id == target_id)
        .order_by(AuditLog.timestamp)
        .all()
    )


def test_the_audit_log_records_who_changed_what_and_when(
    client_p1, staff, clinician, note, seeded_p1
):
    _edit(client_p1, note["id"], staff, V2, 1, "shortened review interval")
    client_p1.post(f"/entries/{note['id']}/revert", json={"to_version": 1}, headers=staff)

    rows = _audit_rows(seeded_p1["db"], note["id"])
    assert [r.action for r in rows] == ["entry.create", "entry.update", "entry.revert"]
    assert {r.actor_id for r in rows} == {"u-a-staff"}
    assert {r.actor_role for r in rows} == {"staff"}
    assert all(r.timestamp is not None for r in rows)
    assert all(r.clinic_id == "clinic-a" for r in rows)


def test_the_audit_log_records_the_version_transition_of_each_change(
    client_p1, staff, note, seeded_p1
):
    """"Who changed what" means little without which version became which."""
    _edit(client_p1, note["id"], staff, V2, 1)
    client_p1.post(f"/entries/{note['id']}/revert", json={"to_version": 1}, headers=staff)

    rows = _audit_rows(seeded_p1["db"], note["id"])
    update = json.loads(rows[1].audit_metadata)
    assert update["previous_version"] == 1 and update["version"] == 2

    revert = json.loads(rows[2].audit_metadata)
    assert revert["version"] == 3 and revert["reverted_from_version"] == 1


def test_the_audit_log_never_contains_the_note_body(
    client_p1, staff, note, seeded_p1
):
    """The hard constraint. The log is a second copy of the record if it holds
    content, and it is the copy nobody applies RBAC to — so it holds lengths
    and ids instead.
    """
    secret = "Patient disclosed a safeguarding concern about a family member."
    _edit(client_p1, note["id"], staff, secret, 1)

    for row in _audit_rows(seeded_p1["db"], note["id"]):
        assert secret not in row.audit_metadata
        assert "safeguarding" not in row.audit_metadata

    update = json.loads(_audit_rows(seeded_p1["db"], note["id"])[1].audit_metadata)
    assert update["content_length"] == len(secret)  # the length, not the text


def test_a_reverted_body_does_not_leak_into_the_log_either(
    client_p1, staff, note, seeded_p1
):
    """Revert copies content between versions and is the easy place to log the
    snapshot 'for debugging'. It must record the length only."""
    secret = "Patient disclosed a safeguarding concern about a family member."
    _edit(client_p1, note["id"], staff, secret, 1)
    _edit(client_p1, note["id"], staff, V3, 2)
    client_p1.post(f"/entries/{note['id']}/revert", json={"to_version": 2}, headers=staff)

    rows = _audit_rows(seeded_p1["db"], note["id"])
    assert all(secret not in r.audit_metadata for r in rows)
    assert json.loads(rows[-1].audit_metadata)["content_length"] == len(secret)


def test_history_is_readable_by_a_role_that_may_read_the_entry(
    client_p1, clinician, note
):
    """A clinician can read staff notes, so they can read the history of one.

    Version history is where a clinician checks whether the words they are
    relying on were changed after the fact — the audit trail is only useful if
    the person making the decision can see it.
    """
    versions = client_p1.get(f"/entries/{note['id']}/versions", headers=clinician)
    assert versions.status_code == 200
    assert versions.json()[0]["content"] == V1


def test_ai_scribed_notes_keep_a_history_that_shows_they_were_not_edited(
    client_p1, clinician
):
    """AI notes are immutable in place (they are corrected by supersede), so
    their history should show exactly one version, forever. That is what makes
    "the machine's words have not been quietly rewritten" checkable rather than
    merely claimed."""
    ai_entry = client_p1.post(
        "/patients/patient-a1/scribe",
        json={"interaction_type": "doctor_patient_consult"},
        headers=clinician,
    ).json()

    versions = client_p1.get(
        f"/entries/{ai_entry['id']}/versions", headers=clinician
    ).json()
    assert len(versions) == 1
    assert versions[0]["edited_by_role"] == "system"

    refused = _edit(client_p1, ai_entry["id"], clinician, "rewritten", 1)
    assert refused.status_code == 403
    assert len(
        client_p1.get(f"/entries/{ai_entry['id']}/versions", headers=clinician).json()
    ) == 1
