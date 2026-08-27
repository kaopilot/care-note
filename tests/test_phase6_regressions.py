"""Phase 6 — regressions for one defect class found during the final polish pass.

THE DEFECT

Every enum-valued column in this schema is declared `Mapped[SomeStrEnum]` but
backed by a `String(20)` column (see `backend/app/models.py`). SQLAlchemy stores
the string and hands back a plain `str` on load — it does not coerce back to the
enum member, because the column type never said it was one.

So for any row read from the database:

    row.status == HighlightStatus.SUGGESTED   ->  True   (StrEnum compares equal)
    row.status is HighlightStatus.SUGGESTED   ->  False  (different objects)

An object created in-session still holds the real member, so `is` works right up
until the first reload. That is why this survived five phases: it is correct in
the unit test that builds the object and wrong in production.

Three places used `is`. All three were live bugs:

  1. highlights.refresh_entry_highlights — the "delete superseded suggestions"
     guard never fired, so every refresh appended a second copy of every
     suggestion. Compounding: an edit or an accept re-runs the refresh.
  2. comment_routes, mention validation — `user.role is not Role.PATIENT` was
     always true, so a patient login could be stored as a mention on an
     internal thread.
  3. comment_routes, task assignment — `assignee.role is Role.PATIENT` was
     always false, so the guard refusing patient assignees never refused one.

Recorded as D-055. The tests below pin the behaviour, and the last two pin the
rule itself so a future `is` comparison against an ORM-loaded enum column fails
the build rather than waiting to be noticed in a screenshot.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.core.enums import HighlightStatus, Role
from app.models import Entry, Highlight
from app.services import highlights as highlight_service


@pytest.fixture()
def clinician(token_for):
    return token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")


# --------------------------------------------------------------------------
# 1 — the mechanism itself, stated as a fact about this schema
# --------------------------------------------------------------------------


def test_enum_columns_come_back_from_the_database_as_plain_strings(seeded_p1):
    """Documents *why* `==` is required throughout.

    If this ever fails because the columns were migrated to a real Enum type,
    the `is` comparisons become safe again and D-055 can be reconsidered — but
    not before.
    """
    db = seeded_p1["db"]
    entry = db.query(Entry).filter(Entry.id == "entry-a1-clin").one()
    highlight_service.refresh_entry_highlights(db, entry)
    db.commit()
    db.expire_all()  # force a genuine reload, which is where the trap springs

    row = db.query(Highlight).filter(Highlight.entry_id == entry.id).first()
    assert row is not None
    assert type(row.status) is str, "status is no longer a bare str; revisit D-055"
    assert row.status == HighlightStatus.SUGGESTED  # equal...
    assert row.status is not HighlightStatus.SUGGESTED  # ...but not identical


# --------------------------------------------------------------------------
# 2 — refreshing twice must not duplicate suggestions
# --------------------------------------------------------------------------


def _spans(db, entry_id: str) -> list[tuple[int, int]]:
    return sorted(
        (h.span_start, h.span_end)
        for h in db.query(Highlight).filter(Highlight.entry_id == entry_id).all()
    )


def test_refreshing_an_entry_twice_does_not_duplicate_its_suggestions(seeded_p1):
    """The Glance View defect: the same claim rendered twice, from two rows.

    Under the old identity check this produced 2N rows after two passes, and the
    Top Card showed every highlight in duplicate — which reads as two
    independent sources agreeing, the opposite of what the provenance model is
    meant to convey.
    """
    db = seeded_p1["db"]
    entry = db.query(Entry).filter(Entry.id == "entry-a1-clin").one()

    highlight_service.refresh_entry_highlights(db, entry)
    db.commit()
    after_first = _spans(db, entry.id)
    assert after_first, "fixture produced no suggestions to begin with"

    # The second pass is what the seed script, an edit, and an accept all do.
    highlight_service.refresh_entry_highlights(db, entry)
    db.commit()
    after_second = _spans(db, entry.id)

    assert after_second == after_first, (
        f"refresh is not idempotent: {len(after_first)} spans became "
        f"{len(after_second)}"
    )
    assert len(after_second) == len(set(after_second)), "duplicate spans present"


def test_refresh_is_idempotent_across_many_passes(seeded_p1):
    """Ten passes, because the real failure mode was unbounded growth rather
    than a single stray copy."""
    db = seeded_p1["db"]
    entry = db.query(Entry).filter(Entry.id == "entry-a1-pt").one()

    highlight_service.refresh_entry_highlights(db, entry)
    db.commit()
    baseline = _spans(db, entry.id)
    # Without this the test passes vacuously on an entry that scores nothing —
    # empty == empty. `entry-a1-staff` was the original choice and did exactly
    # that, which the mutation check caught.
    assert baseline, "fixture entry produces no suggestions; test would be vacuous"

    for _ in range(10):
        highlight_service.refresh_entry_highlights(db, entry)
        db.commit()

    assert _spans(db, entry.id) == baseline


def test_a_clinician_decision_still_survives_refresh(seeded_p1):
    """The fix deletes superseded *suggestions*. It must not touch a row a
    clinician has ruled on — that was the point of the guard in the first place,
    and a fix that over-deletes would erase clinical decisions."""
    db = seeded_p1["db"]
    entry = db.query(Entry).filter(Entry.id == "entry-a1-clin").one()

    highlight_service.refresh_entry_highlights(db, entry)
    db.commit()

    accepted = db.query(Highlight).filter(Highlight.entry_id == entry.id).first()
    accepted.status = HighlightStatus.ACCEPTED
    accepted.decided_by = "u-a-clinician"
    accepted_id = accepted.id
    accepted_span = (accepted.span_start, accepted.span_end)
    db.commit()

    highlight_service.refresh_entry_highlights(db, entry)
    db.commit()

    survivor = db.query(Highlight).filter(Highlight.id == accepted_id).first()
    assert survivor is not None, "refresh deleted a clinician-accepted highlight"
    assert survivor.status == HighlightStatus.ACCEPTED

    # And it must not have been re-suggested alongside itself.
    same_span = [
        h
        for h in db.query(Highlight).filter(Highlight.entry_id == entry.id).all()
        if (h.span_start, h.span_end) == accepted_span
    ]
    assert len(same_span) == 1


def test_the_glance_view_shows_each_claim_once(client_p1, clinician):
    """End to end, at the surface the defect was visible on."""
    client_p1.post("/patients/patient-a1/highlights/refresh", headers=clinician)
    client_p1.post("/patients/patient-a1/highlights/refresh", headers=clinician)

    glance = client_p1.get("/patients/patient-a1/glance", headers=clinician)
    assert glance.status_code == 200

    seen = [
        (h["entry_id"], h["span_start"], h["span_end"])
        for h in glance.json()["highlights"]
    ]
    assert len(seen) == len(set(seen)), f"Top Card repeated a claim: {seen}"


# --------------------------------------------------------------------------
# 3 — patients are not clinical collaborators
# --------------------------------------------------------------------------


def test_a_patient_login_cannot_be_stored_as_a_mention(client_p1, clinician):
    """Patients cannot read internal comments (D-035), so mentioning one is a
    mention that can never resolve for its target — and it surfaces the
    patient's name in the clinical mention list as though they were staff."""
    response = client_p1.post(
        "/entries/entry-a1-staff/comments",
        json={"body": "routing this to the wrong place", "mentions": ["u-a-patient"]},
        headers=clinician,
    )
    assert response.status_code == 201, response.text
    assert response.json()["mentions"] == [], "patient login kept as a mention"


def test_a_staff_mention_is_still_kept(client_p1, clinician):
    """Guard against fixing the bug by dropping every mention."""
    response = client_p1.post(
        "/entries/entry-a1-staff/comments",
        json={"body": "over to you", "mentions": ["u-a-staff"]},
        headers=clinician,
    )
    assert response.status_code == 201, response.text
    assert response.json()["mentions"] == ["u-a-staff"]


def test_a_task_cannot_be_assigned_to_a_patient_login(client_p1, clinician):
    """The handler already carried the right error message; the guard in front
    of it just never fired. Assigning clinical follow-up to the patient's own
    login puts their name in the clinician's 'Open actions' list as the
    responsible party."""
    response = client_p1.post(
        "/patients/patient-a1/tasks",
        json={"description": "arrange repeat bloods", "assigned_to": "u-a-patient"},
        headers=clinician,
    )
    assert response.status_code == 400, response.text
    assert "staff, clinician or admin" in response.json()["detail"]


def test_a_staff_assignee_is_still_accepted(client_p1, clinician):
    """Guard against fixing the bug by refusing everything."""
    response = client_p1.post(
        "/patients/patient-a1/tasks",
        json={"description": "arrange repeat bloods", "assigned_to": "u-a-staff"},
        headers=clinician,
    )
    assert response.status_code == 201, response.text
    assert response.json()["assigned_to_role"] == Role.STAFF


# --------------------------------------------------------------------------
# 4 — the rule, enforced on the source tree
# --------------------------------------------------------------------------

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend" / "app"

ENUM_NAMES = (
    "Role",
    "EntryType",
    "HighlightStatus",
    "TaskStatus",
    "CommentStatus",
    "DecayState",
    "InteractionType",
    "CaptureKind",
    "RiskLevel",
)

# `receiver.attribute is EnumName.MEMBER` — an identity check whose left side is
# an attribute read, which is where an ORM column value comes from. A comparison
# on a bare local (`if kind is CaptureKind.PATIENT`) is not matched: that holds
# whatever the caller passed, and the coercion question belongs at that boundary.
IDENTITY_ON_ATTRIBUTE = re.compile(
    r"\b(?P<recv>[A-Za-z_][A-Za-z0-9_]*)\.(?P<attr>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s+is\s+(?:not\s+)?(?P<enum>" + "|".join(ENUM_NAMES) + r")\."
)

# Receivers whose enum-ness is established at a boundary, not read from a row:
#   scope.* / self.*  — coerced by `Role(raw_role)` in security/rbac.py
#   payload.*         — coerced by pydantic before the handler runs
COERCED_RECEIVERS = {"scope", "self", "payload"}


def test_no_identity_comparison_against_an_orm_loaded_enum():
    """The guard for D-055.

    A String-backed enum column reloads as `str`, so `is` against an enum member
    is always False and fails silently — no exception, no test failure, just a
    branch that stops running. Three of these shipped. This scan is the same
    technique that keeps the LLM chokepoint and the raw-HTML ban honest.
    """
    offenders: list[str] = []
    for path in sorted(BACKEND.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            for match in IDENTITY_ON_ATTRIBUTE.finditer(line):
                if match.group("recv") in COERCED_RECEIVERS:
                    continue
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Identity comparison against an enum member on an attribute that may "
        "have been loaded from the database. Use `==` / `!=` — a String-backed "
        "column returns a plain str and `is` will never match.\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "snippet, should_flag",
    [
        ("if row.status is HighlightStatus.SUGGESTED:", True),
        ("if assignee.role is not Role.PATIENT:", True),
        ("if scope.role is Role.ADMIN:", False),  # coerced in rbac.py
        ("if payload.interaction_type is InteractionType.AI_PATIENT_SESSION:", False),
        ("if kind is CaptureKind.PATIENT:", False),  # bare local, not an attribute
        ("if EntryType(e.type) is EntryType.PATIENT_NOTE:", False),  # explicit coercion
        ("if row.status == HighlightStatus.SUGGESTED:", False),  # the correct form
    ],
)
def test_the_scan_flags_what_it_claims_to(snippet: str, should_flag: bool):
    """A scan nobody has watched fail is not evidence of anything."""
    match = IDENTITY_ON_ATTRIBUTE.search(snippet)
    flagged = bool(match) and match.group("recv") not in COERCED_RECEIVERS
    assert flagged is should_flag
