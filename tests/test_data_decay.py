"""Phase 4 — data decay.

Not a test the brief names, but compression is the only operation in this system
that rewrites stored clinical text, so it gets the same treatment as the ones
that are named.

The properties asserted here are, in order of how badly they would hurt if false:

  1. Cold is reversible. The archived original comes back byte for byte.
  2. Provenance survives compression. Every span pointer still resolves to the
     same words, because offsets index the original rather than the summary.
  3. Nothing safety-critical is compressed, however old it gets.
  4. Nothing with outstanding work attached is compressed.
  5. Cold down-weights, it never hides.
  6. The summary is a subset of what a human wrote, never a paraphrase.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.core.enums import (
    CommentStatus,
    DecayState,
    EntryType,
    HighlightStatus,
    RiskLevel,
    Role,
    TaskStatus,
)
from app.core.provenance import resolve
from app.main import app
from app.models import (
    Comment,
    Entry,
    EntryArchive,
    Highlight,
    Patient,
    Clinic,
    Task,
    User,
    Version,
)
from app.security.auth import create_access_token, hash_password
from app.services import decay, features, scoring
from app.services import highlights as highlight_service

LONG_NOTE = (
    "Annual diabetic review completed today with the patient in clinic. "
    "T2DM remains stable on metformin 1g twice daily with good adherence. "
    "HbA1c 7.1%, essentially unchanged from the previous annual review. "
    "No neuropathy detected on monofilament testing of either foot. "
    "Blood pressure well controlled through the year on amlodipine. "
    "Weight steady and the patient reports walking most evenings. "
    "Continue current management and review again in twelve months."
)

ALLERGY_NOTE = (
    "Historical allergy documentation reviewed and confirmed with the patient. "
    "Penicillin allergy confirmed, presenting as a widespread rash in childhood. "
    "No anaphylaxis and no respiratory involvement was ever recorded. "
    "Patient carries no adrenaline autoinjector and none is indicated. "
    "This has been carried forward on the record since the first registration."
)


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    now = datetime.now(timezone.utc)
    pw = hash_password("pw")
    session.add(Clinic(id="clinic-a", name="Clinic A"))
    session.add(
        Patient(id="pa1", clinic_id="clinic-a", name="Amira Rahman",
                dob="1968-03-11", mrn="MRN-A-1")
    )
    session.add_all(
        [
            User(id="u-a-clin", clinic_id="clinic-a", role=Role.CLINICIAN,
                 name="Dr Lim", username="clinician_a", password_hash=pw),
            User(id="u-a-staff", clinic_id="clinic-a", role=Role.STAFF,
                 name="Nurse Priya", username="staff_a", password_hash=pw),
            User(id="u-a-admin", clinic_id="clinic-a", role=Role.ADMIN,
                 name="Serene", username="admin_a", password_hash=pw),
        ]
    )

    def add(entry_id, content, days, risk=RiskLevel.NONE, etype=EntryType.CLINICIAN_SECTION):
        entry = Entry(
            id=entry_id, patient_id="pa1", clinic_id="clinic-a",
            author_id="u-a-clin", author_role=Role.CLINICIAN, type=etype,
            content=content, title=None, risk_level=risk, version_number=1,
            timestamp=now - timedelta(days=days),
            provenance_pointer=f"entry://{entry_id}",
        )
        session.add(entry)
        session.flush()
        session.add(
            Version(entry_id=entry.id, version_number=1, content_snapshot=content,
                    edited_by="u-a-clin", edited_by_role=Role.CLINICIAN,
                    edited_at=entry.timestamp, change_summary="seeded")
        )
        return entry

    add("e-recent", LONG_NOTE, 3)
    add("e-warm", LONG_NOTE, 90)
    add("e-old", LONG_NOTE, 400)
    add("e-old-allergy", ALLERGY_NOTE, 400)
    add("e-old-highrisk", LONG_NOTE, 400, risk=RiskLevel.HIGH)
    add("e-old-tasked", LONG_NOTE, 400)
    add("e-old-commented", LONG_NOTE, 400)
    add("e-old-short", "Reviewed. No change.", 400)

    session.add(
        Task(id="t1", clinic_id="clinic-a", patient_id="pa1", entry_id="e-old-tasked",
             description="Chase result", assigned_by="u-a-clin", status=TaskStatus.OPEN)
    )
    session.add(
        Comment(id="c1", entry_id="e-old-commented", clinic_id="clinic-a",
                author_id="u-a-clin", author_role=Role.CLINICIAN,
                body="Still open", status=CommentStatus.OPEN, is_internal=True)
    )

    for entry in session.query(Entry).all():
        highlight_service.refresh_entry_highlights(session, entry)
    session.commit()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield {"db": session, "client": client, "now": now}
    app.dependency_overrides.clear()
    session.close()


def auth(user_id: str, role: Role) -> dict[str, str]:
    token = create_access_token(user_id=user_id, role=role, clinic_id="clinic-a")
    return {"Authorization": f"Bearer {token}"}


def states(db) -> dict[str, str]:
    return {entry.id: str(entry.decay_state) for entry in db.query(Entry).all()}


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_the_policy_ages_entries_through_three_states(env):
    db = env["db"]
    decay.run(db, clinic_id="clinic-a", dry_run=False)

    current = states(db)
    assert current["e-recent"] == str(DecayState.HOT)
    assert current["e-warm"] == str(DecayState.WARM)
    assert current["e-old"] == str(DecayState.COLD)


def test_a_dry_run_changes_nothing(env):
    """The default for an operation that rewrites clinical text is to describe it."""
    db = env["db"]
    before = states(db)
    report = decay.run(db, clinic_id="clinic-a", dry_run=True)

    assert report["dry_run"] is True
    assert report["changed"] > 0, "there must be something it declined to do"
    assert states(db) == before


@pytest.mark.parametrize(
    "entry_id,expected_reason",
    [
        ("e-old-allergy", "safety-critical"),
        ("e-old-highrisk", "risk level"),
        ("e-old-tasked", "unresolved tasks"),
        ("e-old-commented", "open comment"),
    ],
)
def test_entries_that_still_matter_are_never_compressed(env, entry_id, expected_reason):
    """Old does not mean settled.

    An allergy from four years ago kills exactly as effectively as one from last
    week, and an unresolved task is the clearest possible statement that
    something is not finished. Each of these is held at warm forever.
    """
    db = env["db"]
    decay.run(db, clinic_id="clinic-a", dry_run=False)

    entry = db.get(Entry, entry_id)
    assert str(entry.decay_state) == str(DecayState.WARM)
    assert entry.content == (ALLERGY_NOTE if entry_id == "e-old-allergy" else LONG_NOTE)

    verdict = decay.classify(db, entry)
    assert verdict.protected is True
    assert expected_reason in verdict.reason


def test_a_clinician_confirmed_highlight_protects_its_entry(env):
    """What a human confirmed matters is exactly what must not be compressed."""
    db = env["db"]
    entry = db.get(Entry, "e-old")
    row = db.query(Highlight).filter(Highlight.entry_id == "e-old").first()
    assert row is not None
    row.status = HighlightStatus.ACCEPTED
    db.commit()

    verdict = decay.classify(db, entry)
    assert verdict.protected is True
    assert "confirmed highlight" in verdict.reason


def test_a_note_too_short_to_compress_is_left_alone(env):
    db = env["db"]
    decay.run(db, clinic_id="clinic-a", dry_run=False)
    entry = db.get(Entry, "e-old-short")
    assert str(entry.decay_state) == str(DecayState.WARM)
    assert entry.content == "Reviewed. No change."


# --------------------------------------------------------------------------
# Compression and the round trip
# --------------------------------------------------------------------------


def test_compression_is_reversible_byte_for_byte(env):
    """The property everything else rests on.

    A lossy archival step in a clinical record is a data-loss bug with a
    scheduler attached, so the round trip is asserted on exact equality rather
    than on similarity.
    """
    db = env["db"]
    decay.run(db, clinic_id="clinic-a", dry_run=False)

    entry = db.get(Entry, "e-old")
    assert str(entry.decay_state) == str(DecayState.COLD)
    assert entry.content != LONG_NOTE
    assert len(entry.content) < len(LONG_NOTE)

    assert decay.archived_original(db, entry) == LONG_NOTE
    assert decay.restore(db, entry) is True
    db.commit()
    assert entry.content == LONG_NOTE
    assert str(entry.decay_state) == str(DecayState.WARM)


def test_the_summary_is_a_subset_of_what_a_human_wrote(env):
    """Extractive, not abstractive.

    Every sentence in a cold entry must be a sentence the author actually
    wrote. An abstractive summariser hallucinating during archival would
    corrupt the record permanently, silently, and at the exact moment nobody is
    looking at it.
    """
    db = env["db"]
    decay.run(db, clinic_id="clinic-a", dry_run=False)
    entry = db.get(Entry, "e-old")

    original_sentences = {text for _, _, text in features.sentences(LONG_NOTE)}
    for _, _, sentence in features.sentences(entry.content):
        assert sentence in original_sentences


def test_compression_reduces_the_hot_read_path_and_reports_its_own_cost(env):
    """The saving is on the read path, and the report says so.

    A single "bytes saved" figure would have flattered the feature: base64
    inflates zlib's output by about a third, so on notes this short the archive
    costs nearly as much as the summary saves. The hot path — what a timeline
    load actually pulls out of `Entry` — is where the win is real, and the
    archive cost is reported beside it rather than netted out of sight.
    """
    db = env["db"]
    report = decay.run(db, clinic_id="clinic-a", dry_run=False)
    assert report["hot_bytes_saved"] > 0
    assert report["hot_bytes_after"] < report["hot_bytes_before"]
    assert report["archive_bytes"] > 0, "the cold copy is not free and must be reported"
    assert "net_storage_delta" in report

    archive = db.query(EntryArchive).filter(EntryArchive.entry_id == "e-old").one()
    assert archive.compression == "zlib+base64"
    assert archive.original_length == len(LONG_NOTE.encode("utf-8"))


def test_restoring_holds_off_the_next_pass(env):
    """A clinician who restores an entry to read it should not find it
    recompressed by morning — that reads as the system arguing with them."""
    db = env["db"]
    decay.run(db, clinic_id="clinic-a", dry_run=False)
    entry = db.get(Entry, "e-old")
    decay.restore(db, entry)
    db.commit()

    decay.run(db, clinic_id="clinic-a", dry_run=False)
    assert db.get(Entry, "e-old").content == LONG_NOTE

    # Past the hold, the policy resumes.
    later = datetime.now(timezone.utc) + timedelta(days=45)
    decay.run(db, clinic_id="clinic-a", dry_run=False, now=later)
    assert str(db.get(Entry, "e-old").decay_state) == str(DecayState.COLD)


# --------------------------------------------------------------------------
# Provenance must survive compression
# --------------------------------------------------------------------------


def test_every_span_pointer_still_resolves_after_compression(env):
    """The defect this was written for.

    Span offsets index the entry's full text. Compressing `Entry.content`
    without redirecting resolution would move every offset onto different words
    — or overrun the end and report a dangling pointer for a highlight that is
    perfectly valid. That would break the requirement Phase 3's
    test_highlight_provenance.py exists to protect.
    """
    db = env["db"]
    before = {
        row.id: resolve(db, row.provenance_pointer, clinic_id="clinic-a")["span_text"]
        for row in db.query(Highlight).all()
    }
    assert before, "fixture must contain span-pointing highlights"

    decay.run(db, clinic_id="clinic-a", dry_run=False)

    for row in db.query(Highlight).all():
        resolved = resolve(db, row.provenance_pointer, clinic_id="clinic-a")
        assert resolved["span_text"] == before[row.id], (
            "a pointer resolved to different words after its entry was compressed"
        )


def test_a_cold_entry_stops_minting_new_spans(env):
    """Two incompatible offset frames in one table is worse than fewer highlights."""
    db = env["db"]
    decay.run(db, clinic_id="clinic-a", dry_run=False)
    entry = db.get(Entry, "e-old")

    created = highlight_service.refresh_entry_highlights(db, entry)
    assert created == []


def test_highlighting_a_compressed_entry_is_refused_not_mis_anchored(env):
    db, client = env["db"], env["client"]
    decay.run(db, clinic_id="clinic-a", dry_run=False)

    response = client.post(
        "/entries/e-old/highlights",
        json={"span_start": 0, "span_end": 20},
        headers=auth("u-a-clin", Role.CLINICIAN),
    )
    assert response.status_code == 400
    assert "restore" in response.json()["detail"].lower()


# --------------------------------------------------------------------------
# Cold down-weights; it never hides
# --------------------------------------------------------------------------


def test_cold_entries_are_down_weighted_but_still_scored(env):
    """SCHEMA.md originally said cold entries were excluded from scoring.

    Building it showed that to be the wrong policy: an entry can be the only
    record of something important and still be four years old. Age is a prior
    about relevance, never a proof of irrelevance — so cold multiplies the
    score by 0.4 rather than by zero (D-042).
    """
    db = env["db"]
    now = datetime.now(timezone.utc)
    kwargs = dict(
        clinic_id="clinic-a", timestamp=now, risk_level=RiskLevel.MEDIUM,
        tags=["med:warfarin"], now=now,
    )
    hot, _ = scoring.score_span(db, decay_state=DecayState.HOT, **kwargs)
    cold, breakdown = scoring.score_span(db, decay_state=DecayState.COLD, **kwargs)

    assert 0 < cold < hot
    assert breakdown["multiplier"] == pytest.approx(0.4)


def test_a_cold_entry_still_appears_in_the_timeline(env):
    db, client = env["db"], env["client"]
    decay.run(db, clinic_id="clinic-a", dry_run=False)

    entries = client.get(
        "/patients/pa1/entries", headers=auth("u-a-clin", Role.CLINICIAN)
    ).json()
    cold = [item for item in entries if item["id"] == "e-old"]
    assert cold, "compression must not remove an entry from the record"
    assert cold[0]["decay_state"] == str(DecayState.COLD)


# --------------------------------------------------------------------------
# Access control on the lifecycle routes
# --------------------------------------------------------------------------


def test_only_admin_may_apply_the_decay_policy(env):
    """Admin is the oversight role (D-011): it cannot author clinical content,
    which makes it the right holder of an operation that rewrites stored text
    without adding any clinical claim to the record."""
    client = env["client"]
    assert client.post(
        "/clinic/decay/run?dry_run=false", headers=auth("u-a-clin", Role.CLINICIAN)
    ).status_code == 403
    assert client.post(
        "/clinic/decay/run?dry_run=false", headers=auth("u-a-staff", Role.STAFF)
    ).status_code == 403
    assert client.post(
        "/clinic/decay/run?dry_run=false", headers=auth("u-a-admin", Role.ADMIN)
    ).status_code == 200


def test_staff_cannot_restore_and_clinicians_can(env):
    db, client = env["db"], env["client"]
    decay.run(db, clinic_id="clinic-a", dry_run=False)

    assert client.post(
        "/entries/e-old/restore", headers=auth("u-a-staff", Role.STAFF)
    ).status_code == 403

    response = client.post(
        "/entries/e-old/restore", headers=auth("u-a-clin", Role.CLINICIAN)
    )
    assert response.status_code == 200
    assert response.json()["content"] == LONG_NOTE


def test_restoring_an_entry_that_is_not_archived_is_a_conflict_not_a_crash(env):
    client = env["client"]
    response = client.post(
        "/entries/e-recent/restore", headers=auth("u-a-clin", Role.CLINICIAN)
    )
    assert response.status_code == 409


def test_the_archive_endpoint_returns_metadata_not_content(env):
    """Reading the original is an audited restore. This must not be a way round it."""
    db, client = env["db"], env["client"]
    decay.run(db, clinic_id="clinic-a", dry_run=False)

    response = client.get(
        "/entries/e-old/archive", headers=auth("u-a-clin", Role.CLINICIAN)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["archived"] is True
    assert body["original_length"] == len(LONG_NOTE.encode("utf-8"))
    assert "monofilament" not in response.text
    assert LONG_NOTE not in response.text


def test_decay_is_clinic_scoped(env):
    """A run must not reach into another clinic's record."""
    db = env["db"]
    db.add(Clinic(id="clinic-b", name="Clinic B"))
    db.add(Patient(id="pb1", clinic_id="clinic-b", name="D", dob="1975-01-01", mrn="M"))
    db.flush()
    other = Entry(
        id="b-old", patient_id="pb1", clinic_id="clinic-b", author_id="x",
        author_role=Role.CLINICIAN, type=EntryType.CLINICIAN_SECTION,
        content=LONG_NOTE, risk_level=RiskLevel.NONE, version_number=1,
        timestamp=datetime.now(timezone.utc) - timedelta(days=400),
        provenance_pointer="entry://b-old",
    )
    db.add(other)
    db.commit()

    decay.run(db, clinic_id="clinic-a", dry_run=False)
    assert str(db.get(Entry, "b-old").decay_state) == str(DecayState.HOT)
    assert db.get(Entry, "b-old").content == LONG_NOTE
