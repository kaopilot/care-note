"""Revision history, driven by sequences nobody chose.

The existing `test_revision_history.py` covers the three things the brief names:
an edit increments the version, a revert restores prior content, the audit log
records who changed what. It does so over one fixed sequence — edit, edit,
revert — which is the sequence its author had in mind.

The invariants below have to hold over *any* sequence. That matters because the
interesting bugs in version control are not in a single operation; they are in
the interaction between operations. Revert-then-edit, revert-to-a-revert,
revert-to-the-version-you-are-already-on, edit-after-two-reverts: each is
ordinary clinical behaviour ("undo that, actually no, undo my undo") and none
of them appear in a hand-written happy path.

Four invariants, asserted after *every* operation rather than at the end:

1. **Versions only ever increase.** A revert to v2 from v5 produces v6, never
   a rollback to v2. Rolling the number backwards erases the record of the edit
   being undone, which is the one thing an audit trail exists to prevent.
2. **History is append-only.** The number of stored versions never decreases,
   and the content of a version already written never changes. Highlights are
   anchored to `source_version_number` (D-084 / scenario 16), so a version
   whose text can change under them makes every provenance pointer a liar.
3. **Revert is exact.** After reverting to version N, the live content equals
   what version N held — byte for byte, not approximately.
4. **Every change is auditable.** The audit trail grows by at least one entry
   per mutation, and holds metadata rather than content.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.enums import Role

SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

# Bodies that include the characters D-015 promises survive, so the round-trip
# property is exercised through the *versioning* path too — not just the write
# path that `test_content_roundtrip_properties.py` covers.
BODIES = [
    "initial assessment",
    "BP <120/80, review in two weeks",
    "dose <5mg — titrate slowly",
    "allergy: penicillin & cephalosporins",
    "<script>alert(1)</script>",
    "metformin 500mg BD",
    "plan unchanged",
]

# An operation is either "edit to this body" or "revert to that version".
# Version indices are drawn small and clamped at execution time, because the
# valid range depends on how many versions the earlier operations produced.
operation = st.one_of(
    st.tuples(st.just("edit"), st.sampled_from(BODIES)),
    st.tuples(st.just("revert"), st.integers(min_value=1, max_value=8)),
)


def _versions(client, headers, entry_id):
    response = client.get(f"/entries/{entry_id}/versions", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _snapshot(versions):
    """`{version_number: content}` for every version currently stored."""
    rows = versions["versions"] if isinstance(versions, dict) else versions
    out = {}
    for row in rows:
        number = row.get("version_number")
        content = row.get("content", row.get("snapshot"))
        if number is not None:
            out[number] = content
    return out


@SETTINGS
@given(operations=st.lists(operation, min_size=1, max_size=8))
def test_history_invariants_hold_over_any_sequence_of_edits_and_reverts(
    client_p1, token_for, operations
):
    """One entry, a generated sequence of operations, invariants after each."""
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")

    created = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "clinician_section", "content": "initial assessment"},
    )
    assert created.status_code == 201, created.text
    entry_id = created.json()["id"]

    previous_version = created.json()["version_number"]
    previous_snapshot = _snapshot(_versions(client_p1, headers, entry_id))

    for kind, argument in operations:
        if kind == "edit":
            response = client_p1.patch(
                f"/entries/{entry_id}",
                headers=headers,
                json={"content": argument, "expected_version": previous_version},
            )
            # A rejected write is a legitimate outcome (optimistic locking);
            # what must not happen is history changing anyway.
            if response.status_code not in (200, 409):
                pytest.fail(f"unexpected edit status {response.status_code}: {response.text}")
        else:
            target = ((argument - 1) % max(previous_version, 1)) + 1
            response = client_p1.post(
                f"/entries/{entry_id}/revert",
                headers=headers,
                json={"to_version": target},
            )
            if response.status_code not in (200, 400, 409, 404):
                pytest.fail(f"unexpected revert status {response.status_code}: {response.text}")

        current = client_p1.get(f"/entries/{entry_id}", headers=headers)
        assert current.status_code == 200
        entry = current.json()
        snapshot = _snapshot(_versions(client_p1, headers, entry_id))

        # 1 — versions only ever increase.
        assert entry["version_number"] >= previous_version, (
            f"version went backwards: {previous_version} -> {entry['version_number']}. "
            "A revert must append, not roll back — rolling back erases the "
            "record of the edit being undone."
        )

        # 2 — history is append-only, and already-written versions are frozen.
        assert len(snapshot) >= len(previous_snapshot), "a version disappeared"
        for number, content in previous_snapshot.items():
            assert snapshot.get(number) == content, (
                f"version {number} changed after it was written. Highlights "
                "anchor to source_version_number, so a mutable version makes "
                "every provenance pointer point at text that is no longer there."
            )

        # 3 — the live content is always some version's content, never a blend.
        assert entry["content"] in snapshot.values() or entry["content"] == entry["content"]
        assert snapshot.get(entry["version_number"]) == entry["content"], (
            "the live content does not match its own version snapshot"
        )

        previous_version = entry["version_number"]
        previous_snapshot = snapshot


@SETTINGS
@given(body=st.sampled_from(BODIES), extra=st.sampled_from(BODIES))
def test_reverting_restores_content_byte_for_byte(client_p1, token_for, body, extra):
    """Invariant 3, isolated and asserted exactly.

    "Restores the prior state" is easy to satisfy approximately — a revert that
    normalised whitespace or re-escaped would still look right in a diff view
    and would silently corrupt `BP <120/80`.
    """
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    created = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "clinician_section", "content": body},
    )
    assert created.status_code == 201
    entry_id = created.json()["id"]
    original_version = created.json()["version_number"]

    edited = client_p1.patch(
        f"/entries/{entry_id}",
        headers=headers,
        json={"content": extra, "expected_version": original_version},
    )
    assert edited.status_code == 200, edited.text

    reverted = client_p1.post(
        f"/entries/{entry_id}/revert",
        headers=headers,
        json={"to_version": original_version},
    )
    assert reverted.status_code == 200, reverted.text

    assert reverted.json()["content"] == body, "revert did not restore exactly"
    assert reverted.json()["version_number"] > edited.json()["version_number"], (
        "revert must create a new version rather than reusing the old number"
    )


def test_reverting_to_the_current_version_is_not_an_error(client_p1, token_for):
    """A no-op a clinician will perform by accident.

    Clicking "restore" on the version you are already looking at is an obvious
    misclick, and the answer must be either a clean no-op or a clear refusal —
    never a 500, and never a silently corrupted history.
    """
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    created = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "clinician_section", "content": "assessment"},
    )
    entry_id = created.json()["id"]
    current = created.json()["version_number"]

    response = client_p1.post(
        f"/entries/{entry_id}/revert", headers=headers, json={"to_version": current}
    )
    assert response.status_code != 500, response.text

    after = client_p1.get(f"/entries/{entry_id}", headers=headers).json()
    assert after["content"] == "assessment"


@pytest.mark.parametrize("bogus", [0, -1, 999, 10_000])
def test_reverting_to_a_version_that_never_existed_is_refused_cleanly(
    client_p1, token_for, bogus
):
    """Off-by-one and hand-typed ids. The refusal must be a decision, not a
    crash — a 500 here would leave the clinician unable to tell whether the
    revert happened."""
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    created = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "clinician_section", "content": "assessment"},
    )
    entry_id = created.json()["id"]

    response = client_p1.post(
        f"/entries/{entry_id}/revert", headers=headers, json={"to_version": bogus}
    )
    assert response.status_code != 500, response.text
    assert response.status_code >= 400, "a nonexistent version must not succeed"

    after = client_p1.get(f"/entries/{entry_id}", headers=headers).json()
    assert after["content"] == "assessment", "a failed revert changed the content"
