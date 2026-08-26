"""Required test 4 of 4 — concurrent editing, and a deterministic resolution.

Named by the brief, which asks for two things:

1. two roles editing **different sections** concurrently must not overwrite each
   other; and
2. where a conflict occurs on the **same section**, the resolution strategy must
   be deterministic.

Most collisions in this build are prevented by construction rather than
resolved: RBAC already partitions who may write which entry type, so staff and
clinicians cannot be editing the same section at all. What is left is two users
of the *same* role on the *same* section, and that is handled with optimistic
locking on `version_number` — the client sends the version it read, and a stale
write is refused with 409 carrying the current state, rather than overwriting
work it never saw. Last-write-wins was the alternative; it is one line shorter
and silently discards a colleague's note, which is the exact failure the brief
describes.

Two kinds of test appear below, and the distinction is deliberate:

* **Interleaved** tests (`read → read → write → write`) are deterministic and
  prove the lost-update property exactly. They are the specification.
* **Genuinely parallel** tests run real threads against a file-backed SQLite
  database with a session per request. They are slower and less precise about
  *which* writer wins, but they exercise the window the interleaved tests
  cannot: both callers passing the version check before either commits.

The parallel tests are why this file exists in the form it does. Interleaving
alone reported everything as correct; under real threads the loser of the race
got an unhandled `IntegrityError` and a 500, because the pre-check is
check-then-act and not a lock. The `uq_entry_version` unique constraint was
already preventing the data loss — nothing was ever silently overwritten — but
the API contract was wrong. Fixed in `_appending_version`, recorded as D-037,
and pinned by `test_the_loser_of_a_real_race_gets_a_conflict_not_a_crash`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base, get_db
from app.core.enums import EntryType, Role, RiskLevel
from app.main import app
from app.models import Clinic, Entry, Patient, User, Version
from app.security.auth import create_access_token, hash_password


@pytest.fixture()
def clinician(token_for):
    return token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")


@pytest.fixture()
def staff(token_for):
    return token_for("u-a-staff", Role.STAFF, "clinic-a")


# --------------------------------------------------------------------------
# Part 1 — different sections, interleaved: no overwriting
# --------------------------------------------------------------------------


def test_two_roles_editing_different_sections_do_not_overwrite_each_other(
    client_p1, clinician, staff, seeded_p1
):
    """The brief's first requirement, in its most literal form.

    Both callers read at version 1, then both write. Interleaving the reads
    before the writes is what makes this a concurrency test rather than two
    sequential edits — if the writes shared any state, the second would clobber
    the first here.
    """
    clinician_before = client_p1.get("/entries/entry-a1-clin", headers=clinician).json()
    staff_before = client_p1.get("/entries/entry-a1-staff", headers=staff).json()
    assert clinician_before["version_number"] == staff_before["version_number"] == 1

    clinician_write = client_p1.patch(
        "/entries/entry-a1-clin",
        json={
            "content": "Assessment updated: add ACE inhibitor, recheck in 2 weeks.",
            "expected_version": clinician_before["version_number"],
        },
        headers=clinician,
    )
    staff_write = client_p1.patch(
        "/entries/entry-a1-staff",
        json={
            "content": "BP 142/88 on recheck. Foot check done, no ulceration.",
            "expected_version": staff_before["version_number"],
        },
        headers=staff,
    )
    assert clinician_write.status_code == 200
    assert staff_write.status_code == 200

    db = seeded_p1["db"]
    clinician_entry = db.get(Entry, "entry-a1-clin")
    staff_entry = db.get(Entry, "entry-a1-staff")

    assert "ACE inhibitor" in clinician_entry.content
    assert "142/88" in staff_entry.content
    assert clinician_entry.version_number == 2
    assert staff_entry.version_number == 2
    # Neither write leaked into the other's row.
    assert "142/88" not in clinician_entry.content
    assert "ACE inhibitor" not in staff_entry.content


def test_each_section_keeps_its_own_independent_version_chain(
    client_p1, clinician, staff
):
    """Version numbers are per entry, not global. A shared counter would make
    every concurrent edit anywhere on the chart look like a conflict."""
    for i in range(3):
        assert client_p1.patch(
            "/entries/entry-a1-staff",
            json={"content": f"Observation revision {i}.", "expected_version": i + 1},
            headers=staff,
        ).status_code == 200

    clinician_entry = client_p1.get("/entries/entry-a1-clin", headers=clinician).json()
    assert clinician_entry["version_number"] == 1, "an unrelated entry moved"

    assert client_p1.patch(
        "/entries/entry-a1-clin",
        json={"content": "Plan revised once.", "expected_version": 1},
        headers=clinician,
    ).status_code == 200


def test_rbac_prevents_most_collisions_before_locking_is_needed(
    client_p1, clinician, staff
):
    """The cheapest conflict resolution is making the conflict impossible.

    Staff and clinicians cannot collide on a section because neither can write
    the other's type at all — so the optimistic lock only ever has to arbitrate
    between peers of the same role.
    """
    assert client_p1.patch(
        "/entries/entry-a1-clin",
        json={"content": "staff editing the plan", "expected_version": 1},
        headers=staff,
    ).status_code == 403
    assert client_p1.patch(
        "/entries/entry-a1-staff",
        json={"content": "clinician editing the observations", "expected_version": 1},
        headers=clinician,
    ).status_code == 403


# --------------------------------------------------------------------------
# Part 2 — same section, interleaved: deterministic resolution
# --------------------------------------------------------------------------


def test_the_second_writer_on_the_same_section_is_refused_not_silently_applied(
    client_p1, token_for, seeded_p1
):
    """Two clinicians, same section, both reading version 1.

    First write wins. Second is refused with 409 — deterministic, and the same
    way round every time.
    """
    first = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    second = token_for("u-b-clinician", Role.CLINICIAN, "clinic-a")  # peer, same clinic

    read_by_first = client_p1.get("/entries/entry-a1-clin", headers=first).json()
    read_by_second = client_p1.get("/entries/entry-a1-clin", headers=second).json()
    assert read_by_first["version_number"] == read_by_second["version_number"] == 1

    winner = client_p1.patch(
        "/entries/entry-a1-clin",
        json={"content": "Plan A: titrate metformin.", "expected_version": 1},
        headers=first,
    )
    loser = client_p1.patch(
        "/entries/entry-a1-clin",
        json={"content": "Plan B: switch to gliclazide.", "expected_version": 1},
        headers=second,
    )

    assert winner.status_code == 200
    assert loser.status_code == 409

    stored = seeded_p1["db"].get(Entry, "entry-a1-clin")
    assert stored.content == "Plan A: titrate metformin."
    assert stored.version_number == 2
    assert "gliclazide" not in stored.content


def test_the_refusal_tells_the_loser_what_they_are_about_to_overwrite(
    client_p1, token_for
):
    """A bare "409, try again" makes the user retype blind and invites them to
    paste over a colleague's work on the second attempt. The body carries the
    current version and content so the UI can show the difference."""
    first = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    second = token_for("u-b-clinician", Role.CLINICIAN, "clinic-a")

    client_p1.patch(
        "/entries/entry-a1-clin",
        json={"content": "Plan A: titrate metformin.", "expected_version": 1},
        headers=first,
    )
    conflict = client_p1.patch(
        "/entries/entry-a1-clin",
        json={"content": "Plan B: switch to gliclazide.", "expected_version": 1},
        headers=second,
    ).json()["detail"]

    assert conflict["error"] == "version_conflict"
    assert conflict["expected_version"] == 1
    assert conflict["current_version"] == 2
    assert conflict["current_content"] == "Plan A: titrate metformin."
    assert conflict["message"].strip()


def test_the_loser_succeeds_after_reloading(client_p1, token_for, seeded_p1):
    """The strategy has to terminate. Refusing forever is not resolution — the
    refused writer reloads, sees the current version, and their next write
    lands."""
    first = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    second = token_for("u-b-clinician", Role.CLINICIAN, "clinic-a")

    client_p1.patch(
        "/entries/entry-a1-clin",
        json={"content": "Plan A: titrate metformin.", "expected_version": 1},
        headers=first,
    )
    refused = client_p1.patch(
        "/entries/entry-a1-clin",
        json={"content": "Plan B: switch to gliclazide.", "expected_version": 1},
        headers=second,
    )
    assert refused.status_code == 409

    current = refused.json()["detail"]["current_version"]
    retried = client_p1.patch(
        "/entries/entry-a1-clin",
        json={
            "content": "Plan A confirmed; add gliclazide if HbA1c stays high.",
            "expected_version": current,
        },
        headers=second,
    )
    assert retried.status_code == 200
    assert retried.json()["version_number"] == 3

    # Both intentions are preserved in history — nothing was lost, only ordered.
    # (The seeded entry starts at version_number 1 without a corresponding
    # Version row, so history begins at 2 here; entries created through the API
    # do get a v1 row — asserted in test_revision_history.py.)
    versions = client_p1.get("/entries/entry-a1-clin/versions", headers=first).json()
    assert [v["version_number"] for v in versions] == [3, 2]
    assert any("titrate metformin" in v["content"] for v in versions)


def test_a_refused_write_leaves_no_trace_at_all(client_p1, token_for, seeded_p1):
    """A rejected edit must not append a version, an audit row, or a partial
    write. Otherwise the history records changes that never happened."""
    from app.models import AuditLog

    first = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    second = token_for("u-b-clinician", Role.CLINICIAN, "clinic-a")

    client_p1.patch(
        "/entries/entry-a1-clin",
        json={"content": "Plan A: titrate metformin.", "expected_version": 1},
        headers=first,
    )
    client_p1.patch(
        "/entries/entry-a1-clin",
        json={"content": "Plan B: switch to gliclazide.", "expected_version": 1},
        headers=second,
    )

    db = seeded_p1["db"]
    versions = db.query(Version).filter(Version.entry_id == "entry-a1-clin").all()
    assert [v.version_number for v in versions] == [2]  # v1 was seeded without a row
    assert all("gliclazide" not in (v.content_snapshot or "") for v in versions)

    audit = db.query(AuditLog).filter(AuditLog.target_id == "entry-a1-clin").all()
    assert [row.actor_id for row in audit] == ["u-a-clinician"]


# --------------------------------------------------------------------------
# Part 3 — genuinely parallel: real threads, real transactions
# --------------------------------------------------------------------------
#
# The fixture below is local to this file rather than added to conftest.py.
# The shared fixtures use one in-memory SQLite session for the whole app, which
# cannot model two transactions racing: a single session serialises everything
# through itself, so the interesting window never opens. These tests need a
# file-backed database and a session per request, which is a different enough
# setup that folding it into the shared fixture would complicate every other
# test to serve four.


@pytest.fixture()
def parallel(tmp_path):
    """A real engine, a session per request, and a chart to fight over."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrency.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    setup = Session()
    setup.add(Clinic(id="clinic-a", name="Clinic A"))
    setup.add(
        Patient(
            id="patient-a1", clinic_id="clinic-a", name="Amira Rahman",
            dob="1968-03-11", mrn="MRN-A-40192",
        )
    )
    password = hash_password("pw")
    for index in range(4):
        setup.add(
            User(
                id=f"u-clinician-{index}", clinic_id="clinic-a", role=Role.CLINICIAN,
                name=f"Dr {index}", username=f"clinician{index}", password_hash=password,
            )
        )
    setup.add(
        User(
            id="u-staff", clinic_id="clinic-a", role=Role.STAFF, name="Nurse Priya",
            username="staff", password_hash=password,
        )
    )

    sections = (
        ("entry-clin", Role.CLINICIAN, EntryType.CLINICIAN_SECTION, "Plan v1", "u-clinician-0"),
        ("entry-staff", Role.STAFF, EntryType.STAFF_NOTE, "Observations v1", "u-staff"),
    )
    for entry_id, role, entry_type, content, author in sections:
        setup.add(
            Entry(
                id=entry_id, patient_id="patient-a1", clinic_id="clinic-a",
                author_id=author, author_role=role, type=entry_type, content=content,
                risk_level=RiskLevel.NONE, version_number=1,
                provenance_pointer=f"entry://{entry_id}",
            )
        )
        setup.flush()
        setup.add(
            Version(
                entry_id=entry_id, version_number=1, content_snapshot=content,
                edited_by=author, edited_by_role=role, change_summary="created",
            )
        )
    setup.commit()
    setup.close()

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client, Session
    app.dependency_overrides.clear()
    engine.dispose()


def _headers(user_id: str, role: Role) -> dict[str, str]:
    token = create_access_token(user_id=user_id, role=role, clinic_id="clinic-a")
    return {"Authorization": f"Bearer {token}"}


def test_parallel_edits_to_different_sections_both_succeed(parallel):
    """Two roles, two sections, genuinely at the same time. Both land."""
    client, Session = parallel

    def write(which: str):
        if which == "clinician":
            return client.patch(
                "/entries/entry-clin",
                json={"content": "Plan updated by clinician", "expected_version": 1},
                headers=_headers("u-clinician-0", Role.CLINICIAN),
            )
        return client.patch(
            "/entries/entry-staff",
            json={"content": "Observations updated by staff", "expected_version": 1},
            headers=_headers("u-staff", Role.STAFF),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["clinician", "staff"]))

    assert [r.status_code for r in results] == [200, 200]

    session = Session()
    try:
        assert session.get(Entry, "entry-clin").content == "Plan updated by clinician"
        assert session.get(Entry, "entry-staff").content == "Observations updated by staff"
        assert session.get(Entry, "entry-clin").version_number == 2
        assert session.get(Entry, "entry-staff").version_number == 2
    finally:
        session.close()


def test_the_loser_of_a_real_race_gets_a_conflict_not_a_crash(parallel):
    """The regression test for D-037.

    Four clinicians read version 1 and all write at once. Before the fix, the
    pre-check let all four through, the unique constraint refused the late
    writers, and they surfaced as 500s. The data was never at risk — the
    constraint saw to that — but a 500 tells the user nothing and carries none
    of the state they need to recover.
    """
    client, _ = parallel

    def write(index: int):
        return client.patch(
            "/entries/entry-clin",
            json={
                "content": f"Plan proposed by clinician {index}",
                "expected_version": 1,
            },
            headers=_headers(f"u-clinician-{index}", Role.CLINICIAN),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(4)))

    codes = [r.status_code for r in results]
    assert 500 not in codes, f"a lost race surfaced as a crash: {codes}"
    assert sorted(codes) == [200, 409, 409, 409], codes

    # Every refusal is the same shape, whichever path detected it.
    for response in results:
        if response.status_code == 409:
            detail = response.json()["detail"]
            assert detail["error"] == "version_conflict"
            assert detail["current_version"] == 2
            assert detail["current_content"].startswith("Plan proposed by clinician")


def test_a_real_race_never_loses_a_write_or_duplicates_a_version(parallel):
    """The property that actually matters underneath the status codes.

    Exactly one version 2 exists, its content belongs to exactly one of the
    four writers, and the other three contributed nothing — no interleaved
    text, no orphaned version rows.
    """
    client, Session = parallel

    def write(index: int):
        return client.patch(
            "/entries/entry-clin",
            json={
                "content": f"Plan proposed by clinician {index}",
                "expected_version": 1,
            },
            headers=_headers(f"u-clinician-{index}", Role.CLINICIAN),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(4)))

    session = Session()
    try:
        versions = (
            session.query(Version)
            .filter(Version.entry_id == "entry-clin")
            .order_by(Version.version_number)
            .all()
        )
        assert [v.version_number for v in versions] == [1, 2], "history forked"

        entry = session.get(Entry, "entry-clin")
        assert entry.version_number == 2
        assert entry.content == versions[-1].content_snapshot
        # The winner's text is exactly one writer's, not a blend of several.
        assert entry.content in {
            f"Plan proposed by clinician {index}" for index in range(4)
        }
    finally:
        session.close()


def test_parallel_reverts_never_crash_or_fork_the_history(parallel):
    """Revert appends a version exactly as an edit does, so it races the same
    way — but it does *not* resolve the same way, and the difference is worth
    stating rather than papering over.

    `revert` takes no `expected_version`: it names a target version, not a base.
    So a second reverter that reads *after* the first has committed is not
    stale — it performs a valid sequential revert to the same target, and
    legitimately returns 200. Only a reverter that read the *same* base and lost
    the commit race gets a 409. Both outcomes are correct, which means the
    number of 200s here is genuinely non-deterministic and asserting on it would
    make this test flaky rather than strict. (It did, on the first draft.)

    What *is* invariant, and is what actually matters: nothing crashes, the
    version chain stays contiguous with no duplicates, and the content lands on
    the target every time — reverting to v1 twice still means v1.
    """
    client, Session = parallel

    client.patch(
        "/entries/entry-clin",
        json={"content": "Plan v2", "expected_version": 1},
        headers=_headers("u-clinician-0", Role.CLINICIAN),
    )

    def revert(index: int):
        return client.post(
            "/entries/entry-clin/revert",
            json={"to_version": 1},
            headers=_headers(f"u-clinician-{index}", Role.CLINICIAN),
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(revert, range(3)))

    codes = [r.status_code for r in results]
    assert 500 not in codes, f"a revert race surfaced as a crash: {codes}"
    assert all(code in (200, 409) for code in codes), codes
    assert 200 in codes, f"every reverter was refused: {codes}"

    session = Session()
    try:
        numbers = sorted(
            v.version_number
            for v in session.query(Version).filter(Version.entry_id == "entry-clin").all()
        )
        # Contiguous from 1, no duplicates, no gaps — however many landed.
        assert numbers == list(range(1, len(numbers) + 1)), numbers
        assert len(numbers) == 3 + codes.count(200) - 1, (numbers, codes)

        entry = session.get(Entry, "entry-clin")
        assert entry.content == "Plan v1"
        assert entry.version_number == numbers[-1]
    finally:
        session.close()
