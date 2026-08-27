"""Phase 4 — self-learning importance.

The brief allows this test to be conceptual. It is not: the headline scenario
runs end to end through the real API, and the scoring assertions are made
against stored `Highlight.score` values that the Glance View actually reads.

WHAT IS SIMULATED HERE (implemented, asserted):
  * A clinician manually highlights a phrase inside an AI-scribed note.
  * Content elsewhere in the clinic sharing that phrase's feature tags scores
    measurably higher afterwards, with the lift visible in the `learned` term of
    the persisted score breakdown.
  * Accept reinforces, reject dampens, and both arrive through the real
    `POST /highlights/{id}/accept|reject` routes.
  * Learning is clinic-scoped, role-scoped, bounded, and reproducible from the
    interaction log alone.
  * Safety-critical vocabulary cannot be trained into silence.

WHAT A FULL SIMULATION WOULD ADDITIONALLY COVER (not asserted here):
  * A cohort effect over real time — many clinicians across months, where the
    90-day evidence half-life does visible work. Every test below pins `now`
    or runs inside one wall-clock second, so decay is exercised arithmetically
    (`test_evidence_decays_with_age`) rather than observed longitudinally.
  * Whether promoted content actually shortens time-to-decision for a
    clinician. That is the outcome the feature exists for and it is not
    measurable from inside the system; it needs instrumented users, and no
    unit test can stand in for that.
  * Adversarial behaviour — a single clinician repeatedly confirming their own
    hobby-horse. Saturation bounds the damage (asserted in
    `test_weights_saturate_and_cannot_dominate`) but there is no per-user
    normalisation, and at real volume there should be.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from datetime import datetime, timedelta, timezone

from app.core.db import Base, get_db
from app.core.enums import EntryType, InteractionAction, RiskLevel, Role
from app.main import app
from app.models import Clinic, Entry, FeatureWeight, Highlight, Patient, User, Version
from app.security.auth import create_access_token, hash_password
from app.services import features, learning, scoring
from app.services import highlights as highlight_service
from app.services.interactions import record_interaction

# --------------------------------------------------------------------------
# Fixture
#
# Local to this file rather than in conftest.py, following the precedent set in
# Phase 1 (D-028) and Phase 3: this fixture needs entries that deliberately
# share clinical vocabulary across patients and clinics, which is a shape no
# other suite wants and which would make the shared fixtures harder to read for
# everyone else.
# --------------------------------------------------------------------------

# Two entries in clinic A that share `med:warfarin` / `medclass:anticoagulant`,
# on DIFFERENT patients — the transfer being tested is clinic-wide vocabulary,
# not "this chart got busier".
AI_NOTE = (
    "Anticoagulation reviewed. Patient is established on warfarin for atrial "
    "fibrillation. Reports easy bruising on the forearms over two weeks. "
    "Agreed to recheck INR before the next visit."
)
OTHER_PATIENT_NOTE = (
    "Medication reconciliation completed. Warfarin dose unchanged at 3mg daily. "
    "Patient counselled on bleeding risk and when to seek help."
)
# The control. Produces a real, scored highlight, but shares no feature tag with
# the warfarin content — so "did the right thing move?" and "did everything move?"
# are distinguishable. A control that generates no highlight at all would make
# the comparison vacuous.
UNRELATED_NOTE = (
    "Ankle sprain after football, presenting with lateral swelling. "
    "Ibuprofen 400mg as required was advised. Neurovascularly intact."
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

    session.add_all(
        [Clinic(id="clinic-a", name="Clinic A"), Clinic(id="clinic-b", name="Clinic B")]
    )
    session.add_all(
        [
            Patient(id="pa1", clinic_id="clinic-a", name="Amira Rahman",
                    dob="1968-03-11", mrn="MRN-A-1"),
            Patient(id="pa2", clinic_id="clinic-a", name="Marcus Teo",
                    dob="1991-07-24", mrn="MRN-A-2"),
            Patient(id="pb1", clinic_id="clinic-b", name="Daniel Choo",
                    dob="1975-11-02", mrn="MRN-B-1"),
        ]
    )
    session.add_all(
        [
            User(id="u-a-clin", clinic_id="clinic-a", role=Role.CLINICIAN,
                 name="Dr Lim", username="clinician_a", password_hash=pw),
            User(id="u-a-staff", clinic_id="clinic-a", role=Role.STAFF,
                 name="Nurse Priya", username="staff_a", password_hash=pw),
            User(id="u-a-admin", clinic_id="clinic-a", role=Role.ADMIN,
                 name="Serene", username="admin_a", password_hash=pw),
            User(id="u-a-pt", clinic_id="clinic-a", role=Role.PATIENT,
                 name="Amira Rahman", username="patient_a", password_hash=pw,
                 patient_id="pa1"),
            User(id="u-b-clin", clinic_id="clinic-b", role=Role.CLINICIAN,
                 name="Dr Faizal", username="clinician_b", password_hash=pw),
        ]
    )

    def add_entry(entry_id, patient_id, clinic_id, author_id, role, etype, content, days):
        entry = Entry(
            id=entry_id, patient_id=patient_id, clinic_id=clinic_id,
            author_id=author_id, author_role=role, type=etype, content=content,
            title=None, risk_level=RiskLevel.NONE, version_number=1,
            timestamp=now - timedelta(days=days),
            provenance_pointer=f"entry://{entry_id}",
        )
        session.add(entry)
        session.flush()
        session.add(
            Version(entry_id=entry.id, version_number=1, content_snapshot=content,
                    edited_by=author_id, edited_by_role=role,
                    edited_at=entry.timestamp, change_summary="seeded")
        )
        return entry

    add_entry("a-ai", "pa1", "clinic-a", "system", Role.SYSTEM,
              EntryType.AI_DOCTOR_CONSULT_SUMMARY, AI_NOTE, 2)
    add_entry("a-other", "pa2", "clinic-a", "u-a-staff", Role.STAFF,
              EntryType.STAFF_NOTE, OTHER_PATIENT_NOTE, 2)
    add_entry("a-unrelated", "pa2", "clinic-a", "u-a-staff", Role.STAFF,
              EntryType.STAFF_NOTE, UNRELATED_NOTE, 2)
    add_entry("b-other", "pb1", "clinic-b", "u-b-clin", Role.CLINICIAN,
              EntryType.CLINICIAN_SECTION, OTHER_PATIENT_NOTE, 2)

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


def auth(user_id: str, role: Role, clinic_id: str) -> dict[str, str]:
    token = create_access_token(user_id=user_id, role=role, clinic_id=clinic_id)
    return {"Authorization": f"Bearer {token}"}


CLIN_A = ("u-a-clin", Role.CLINICIAN, "clinic-a")
STAFF_A = ("u-a-staff", Role.STAFF, "clinic-a")
CLIN_B = ("u-b-clin", Role.CLINICIAN, "clinic-b")


def highlight_for(db, entry_id: str, needle: str) -> Highlight:
    """The highlight covering a specific phrase.

    Tests address one span by name rather than taking the top-scoring highlight
    on an entry. Ranking by score is exactly what these tests perturb, so
    "the best one" is a moving target: an assertion written against it can pass
    because an unrelated span overtook, which is not the claim being made.
    """
    rows = db.query(Highlight).filter(Highlight.entry_id == entry_id).all()
    matches = [row for row in rows if needle.lower() in (row.span_text or "").lower()]
    assert matches, (
        f"no highlight covering {needle!r} on {entry_id}; "
        f"present: {[row.span_text for row in rows]}"
    )
    return matches[0]


def score_for(db, entry_id: str, needle: str) -> float:
    return highlight_for(db, entry_id, needle).score


def learned_for(db, entry_id: str, needle: str) -> float:
    row = highlight_for(db, entry_id, needle)
    return scoring.decode_breakdown(row.score_breakdown).get("learned", 0.0)


# The phrases each test addresses. WARFARIN_A and WARFARIN_B are the transfer
# pair: different patients, different clinics, same clinical vocabulary.
WARFARIN_AI = "established on warfarin"
WARFARIN_OTHER = "Warfarin dose unchanged"
UNRELATED = "Ibuprofen"


def warfarin_span(content: str) -> tuple[int, int]:
    """Offsets of the sentence mentioning warfarin, as a clinician would select it."""
    for start, end, sentence in features.sentences(content):
        if "warfarin" in sentence.lower():
            return start, end
    raise AssertionError("fixture no longer contains a warfarin sentence")


# --------------------------------------------------------------------------
# 1. The headline requirement, end to end
# --------------------------------------------------------------------------


def test_manual_highlight_in_an_ai_note_promotes_similar_content_elsewhere(env):
    """The brief's scenario, run for real.

    A clinician hand-marks a warfarin phrase inside an AI-scribed summary on one
    patient. A different patient's note in the same clinic, sharing that
    vocabulary, must score higher afterwards.
    """
    db, client = env["db"], env["client"]

    before = score_for(db, "a-other", WARFARIN_OTHER)
    control_before = score_for(db, "a-unrelated", UNRELATED)
    assert before > 0, "fixture must produce a scored highlight to move"
    assert control_before > 0, "the control must be a real highlight, not an absence"
    assert learned_for(db, "a-other", WARFARIN_OTHER) == 0.0, "nothing learned yet"

    start, end = warfarin_span(AI_NOTE)
    response = client.post(
        "/entries/a-ai/highlights",
        json={"span_start": start, "span_end": end},
        headers=auth(*CLIN_A),
    )
    assert response.status_code == 201
    assert "med:warfarin" in response.json()["feature_tags"]

    # The clinician's own patient is rescored on the write path; the other
    # patient in the same clinic is caught by the clinic-wide rebuild, which is
    # the nightly job in production and an endpoint here (D-040).
    assert client.post("/clinic/learning/rebuild", headers=auth(*CLIN_A)).status_code == 200

    after = score_for(db, "a-other", WARFARIN_OTHER)
    assert after > before, (
        f"similar content should be promoted after a manual highlight: "
        f"{before} -> {after}"
    )
    assert learned_for(db, "a-other", WARFARIN_OTHER) > 0.0, (
        "the lift must be attributable to the learned term, not to recency drift"
    )
    assert score_for(db, "a-unrelated", UNRELATED) == pytest.approx(
        control_before, abs=1e-6
    ), "content sharing no tags with the highlighted phrase must not move"


def test_the_lift_is_visible_in_the_persisted_breakdown(env):
    """The clinician can see *why* something moved, not just that it did.

    The breakdown is stored on the Highlight and rendered in the Glance View. A
    ranking that shifts with no inspectable reason is the failure mode this
    product exists to argue against, so the learned term is asserted to be a
    real, separate, non-zero line item.
    """
    db, client = env["db"], env["client"]
    start, end = warfarin_span(AI_NOTE)
    client.post(
        "/entries/a-ai/highlights",
        json={"span_start": start, "span_end": end},
        headers=auth(*CLIN_A),
    )
    client.post("/clinic/learning/rebuild", headers=auth(*CLIN_A))

    row = highlight_for(db, "a-other", WARFARIN_OTHER)
    breakdown = scoring.decode_breakdown(row.score_breakdown)
    assert breakdown["learned"] > 0
    assert set(breakdown) >= {"recency", "risk", "entities", "open_actions", "learned"}


# --------------------------------------------------------------------------
# 2. Accept and reject as signal
# --------------------------------------------------------------------------


def test_accepting_a_suggestion_reinforces_its_tags(env):
    db, client = env["db"], env["client"]
    suggestion = highlight_for(db, "a-ai", WARFARIN_AI)

    before = score_for(db, "a-other", WARFARIN_OTHER)
    assert client.post(
        f"/highlights/{suggestion.id}/accept", headers=auth(*CLIN_A)
    ).status_code == 200
    client.post("/clinic/learning/rebuild", headers=auth(*CLIN_A))

    assert score_for(db, "a-other", WARFARIN_OTHER) > before
    assert learned_for(db, "a-other", WARFARIN_OTHER) > 0


def test_rejecting_a_suggestion_dampens_its_tags(env):
    """A dismissed suggestion must make similar content rank *lower*.

    This is the half that makes the loop a loop. A system that only reinforces
    learns that everything matters, which is the same as learning nothing.
    """
    db, client = env["db"], env["client"]
    suggestion = highlight_for(db, "a-ai", WARFARIN_AI)
    assert "med:warfarin" in highlight_service.decode_tags(suggestion.feature_tags)

    before = score_for(db, "a-other", WARFARIN_OTHER)
    assert client.post(
        f"/highlights/{suggestion.id}/reject", headers=auth(*CLIN_A)
    ).status_code == 200
    client.post("/clinic/learning/rebuild", headers=auth(*CLIN_A))

    assert score_for(db, "a-other", WARFARIN_OTHER) < before
    assert learned_for(db, "a-other", WARFARIN_OTHER) < 0


def test_a_rejected_suggestion_is_never_re_suggested(env):
    """Dampening is not the only thing a rejection buys — the row survives.

    Regeneration reads rejected rows and refuses to propose the same span
    again. Without this the ranking would quietly re-offer, tomorrow, exactly
    what a clinician dismissed today.
    """
    db, client = env["db"], env["client"]
    suggestion = highlight_for(db, "a-ai", WARFARIN_AI)
    span = (suggestion.span_start, suggestion.span_end)

    client.post(f"/highlights/{suggestion.id}/reject", headers=auth(*CLIN_A))
    entry = db.get(Entry, "a-ai")
    highlight_service.refresh_entry_highlights(db, entry)
    db.commit()

    live = db.query(Highlight).filter(Highlight.entry_id == "a-ai").all()
    at_span = [row for row in live if (row.span_start, row.span_end) == span]
    assert len(at_span) == 1, "regeneration must not add a second row at a decided span"
    assert str(at_span[0].status) == "rejected"


# --------------------------------------------------------------------------
# 3. Boundaries: who trains it, and what it is allowed to do
# --------------------------------------------------------------------------


def test_learning_is_clinic_scoped(env):
    """Clinic A's habits must not move clinic B's ranking.

    Prioritisation is derived from behaviour, so leaking it across a tenancy
    boundary would leak one clinic's clinical attention into another — a
    quieter version of the same failure as leaking a note.
    """
    db, client = env["db"], env["client"]
    before_b = score_for(db, "b-other", WARFARIN_OTHER)

    start, end = warfarin_span(AI_NOTE)
    client.post(
        "/entries/a-ai/highlights",
        json={"span_start": start, "span_end": end},
        headers=auth(*CLIN_A),
    )
    client.post("/clinic/learning/rebuild", headers=auth(*CLIN_A))
    client.post("/clinic/learning/rebuild", headers=auth(*CLIN_B))

    assert score_for(db, "b-other", WARFARIN_OTHER) == pytest.approx(before_b, abs=1e-6)
    assert db.query(FeatureWeight).filter(FeatureWeight.clinic_id == "clinic-b").count() == 0


def test_one_clinics_habits_do_not_contaminate_anothers_weights(env):
    """Clinic B's rejections must not drag clinic A's weight negative.

    The previous test proves clinic B's *scores* do not move. This one proves
    the stronger property underneath it: clinic A's weight is computed only from
    clinic A's evidence. Written after mutation checking found that removing the
    clinic filter from the evidence read in `recompute_tags` broke nothing —
    the write was still scoped, so nothing leaked in a fixture where only one
    clinic had any history. A tenancy control that is only tested against an
    empty neighbour is not tested.
    """
    db = env["db"]

    for index in range(6):
        record_interaction(
            db, user_id="u-b-clin", user_role=Role.CLINICIAN, clinic_id="clinic-b",
            action=InteractionAction.REJECT_HIGHLIGHT, target_type="highlight",
            target_id=f"b{index}", tags=["med:warfarin"],
        )
    record_interaction(
        db, user_id="u-a-clin", user_role=Role.CLINICIAN, clinic_id="clinic-a",
        action=InteractionAction.MANUAL_HIGHLIGHT, target_type="entry",
        target_id="a-ai", tags=["med:warfarin"],
    )
    db.commit()

    def weight(clinic_id: str) -> float:
        row = (
            db.query(FeatureWeight)
            .filter(
                FeatureWeight.clinic_id == clinic_id,
                FeatureWeight.feature_tag == "med:warfarin",
            )
            .one()
        )
        return row.weight

    assert weight("clinic-a") > 0, "clinic A confirmed warfarin once; it should promote"
    assert weight("clinic-b") < 0, "clinic B dismissed it six times; it should dampen"

    # And the same must hold after a full rebuild from the log.
    learning.rebuild_clinic(db, "clinic-a")
    learning.rebuild_clinic(db, "clinic-b")
    db.commit()
    assert weight("clinic-a") > 0
    assert weight("clinic-b") < 0


def test_patient_behaviour_does_not_train_the_clinician_ranking(env):
    db = env["db"]
    record_interaction(
        db, user_id="u-a-pt", user_role=Role.PATIENT, clinic_id="clinic-a",
        action=InteractionAction.MANUAL_HIGHLIGHT, target_type="entry",
        target_id="a-ai", tags=["med:warfarin"],
    )
    db.commit()
    assert learning.top_weights(db, "clinic-a") == []


def test_authoring_a_note_is_recorded_but_not_learned_from(env):
    """CREATE is volume, not attention (D-039)."""
    db, client = env["db"], env["client"]
    response = client.post(
        "/patients/pa1/entries",
        json={
            "type": "staff_note",
            "content": "Warfarin dose reviewed again today, no change made.",
        },
        headers=auth(*STAFF_A),
    )
    assert response.status_code == 201

    from app.models import InteractionLog

    logged = db.query(InteractionLog).filter(InteractionLog.action == "create").count()
    assert logged == 1, "the behavioural history still records it"
    assert learning.top_weights(db, "clinic-a") == [], "but it moves no weight"


def test_container_tags_are_not_learnable(env):
    """`type:staff_note` on every staff note would drift into 'staff notes matter'."""
    db = env["db"]
    record_interaction(
        db, user_id="u-a-clin", user_role=Role.CLINICIAN, clinic_id="clinic-a",
        action=InteractionAction.MANUAL_HIGHLIGHT, target_type="entry", target_id="a-ai",
        tags=["type:staff_note", "source:human", "signal:clinician_correction", "med:warfarin"],
    )
    db.commit()
    learned = {row["feature_tag"] for row in learning.top_weights(db, "clinic-a")}
    assert learned == {"med:warfarin"}


def test_learning_cannot_invent_a_highlight_only_move_one(env):
    """A span with no clinical reason stays off the card at any weight.

    The reason check runs before scoring, so even an absurdly reinforced tag
    cannot conjure a suggestion out of prose the rule layer found nothing in.
    """
    db = env["db"]
    for index in range(40):
        record_interaction(
            db, user_id="u-a-clin", user_role=Role.CLINICIAN, clinic_id="clinic-a",
            action=InteractionAction.MANUAL_HIGHLIGHT, target_type="entry",
            target_id=f"x{index}", tags=["type:staff_note", "source:human"],
        )
    db.commit()

    entry = Entry(
        id="a-empty", patient_id="pa1", clinic_id="clinic-a", author_id="u-a-staff",
        author_role=Role.STAFF, type=EntryType.STAFF_NOTE,
        content="Patient attended. Chart updated accordingly. Nothing further today.",
        risk_level=RiskLevel.NONE, version_number=1,
        timestamp=datetime.now(timezone.utc), provenance_pointer="entry://a-empty",
    )
    db.add(entry)
    db.flush()
    created = highlight_service.refresh_entry_highlights(db, entry)
    assert created == []


def test_safety_critical_vocabulary_cannot_be_trained_into_silence(env):
    """Dismissing warfarin should quieten warfarin. Dismissing anaphylaxis must not.

    The asymmetry is deliberate (D-041): the cost of a missed allergy is not
    symmetric with the cost of one extra line on a card, so the learning rule is
    not symmetric either.
    """
    db = env["db"]
    for index in range(8):
        record_interaction(
            db, user_id="u-a-clin", user_role=Role.CLINICIAN, clinic_id="clinic-a",
            action=InteractionAction.REJECT_HIGHLIGHT, target_type="highlight",
            target_id=f"h{index}", tags=["entity:allergy", "med:warfarin"],
        )
    db.commit()

    weights = {row["feature_tag"]: row for row in learning.top_weights(db, "clinic-a")}
    assert weights["med:warfarin"]["weight"] < 0, "ordinary vocabulary dampens"
    assert weights["entity:allergy"]["weight"] == 0.0, "safety vocabulary is floored"
    assert weights["entity:allergy"]["negative_signals"] == 8, "the evidence is still visible"
    assert weights["entity:allergy"]["floored"] is True


def test_weights_saturate_and_cannot_dominate(env):
    """No amount of repetition lets one tag take over the ranking."""
    db = env["db"]
    for index in range(200):
        record_interaction(
            db, user_id="u-a-clin", user_role=Role.CLINICIAN, clinic_id="clinic-a",
            action=InteractionAction.MANUAL_HIGHLIGHT, target_type="entry",
            target_id=f"e{index}", tags=["med:warfarin"],
        )
    db.commit()

    weight = db.query(FeatureWeight).filter(
        FeatureWeight.feature_tag == "med:warfarin"
    ).one().weight
    assert weight < 1.0

    _, breakdown = scoring.score_span(
        db, clinic_id="clinic-a", timestamp=datetime.now(timezone.utc),
        risk_level=RiskLevel.NONE, tags=["med:warfarin"],
    )
    assert breakdown["learned"] <= scoring.W_LEARNED


# --------------------------------------------------------------------------
# 4. The weights are a cache, not a second source of truth
# --------------------------------------------------------------------------


def test_rebuilding_from_the_log_reproduces_the_incremental_weights(env):
    """`FeatureWeight` must be derivable from `InteractionLog` alone.

    If the incremental path and the batch path could disagree, the weights
    would become an unauditable second record of clinician behaviour. This is
    the assertion that keeps them one record with a cache in front of it.
    """
    db, client = env["db"], env["client"]
    start, end = warfarin_span(AI_NOTE)
    client.post(
        "/entries/a-ai/highlights",
        json={"span_start": start, "span_end": end},
        headers=auth(*CLIN_A),
    )
    suggestion = db.query(Highlight).filter(
        Highlight.entry_id == "a-other", Highlight.status == "suggested"
    ).first()
    if suggestion:
        client.post(f"/highlights/{suggestion.id}/reject", headers=auth(*CLIN_A))

    incremental = {
        row.feature_tag: row.weight
        for row in db.query(FeatureWeight).filter(FeatureWeight.clinic_id == "clinic-a")
    }
    assert incremental, "the incremental path must have written something to compare"

    now = datetime.now(timezone.utc)
    learning.rebuild_clinic(db, "clinic-a", now=now)
    db.commit()
    rebuilt = {
        row.feature_tag: row.weight
        for row in db.query(FeatureWeight).filter(FeatureWeight.clinic_id == "clinic-a")
    }

    assert set(incremental) == set(rebuilt)
    for tag, weight in incremental.items():
        assert rebuilt[tag] == pytest.approx(weight, abs=1e-3)


def test_evidence_decays_with_age(env):
    """A signal from a year ago must count for less than one from today.

    Without decay, "learning" is accumulation: a clinic could never stop caring
    about something a previous cohort of clinicians cared about.
    """
    db = env["db"]
    now = datetime.now(timezone.utc)

    row = record_interaction(
        db, user_id="u-a-clin", user_role=Role.CLINICIAN, clinic_id="clinic-a",
        action=InteractionAction.MANUAL_HIGHLIGHT, target_type="entry",
        target_id="a-ai", tags=["med:warfarin"], learn=False,
    )
    row.timestamp = now
    db.commit()
    fresh = learning.rebuild_clinic(db, "clinic-a", now=now)["med:warfarin"]

    row.timestamp = now - timedelta(days=365)
    db.commit()
    stale = learning.rebuild_clinic(db, "clinic-a", now=now)["med:warfarin"]

    assert 0 < stale < fresh
    assert stale < fresh / 4, "a year is four half-lives; the signal should be most gone"


def test_a_fully_decayed_tag_does_not_keep_a_stale_weight(env):
    db = env["db"]
    now = datetime.now(timezone.utc)
    record_interaction(
        db, user_id="u-a-clin", user_role=Role.CLINICIAN, clinic_id="clinic-a",
        action=InteractionAction.VIEW, target_type="entry", target_id="a-ai",
        tags=["med:warfarin"],
    )
    db.commit()
    # VIEW carries weight 0.0, so no evidence exists and no row should survive.
    learning.rebuild_clinic(db, "clinic-a", now=now)
    db.commit()
    assert db.query(FeatureWeight).filter(FeatureWeight.clinic_id == "clinic-a").count() == 0


# --------------------------------------------------------------------------
# 5. The transparency surface
# --------------------------------------------------------------------------


def test_clinicians_can_read_what_the_system_learned(env):
    """A ranker that adapts to you and will not say how is the thing this
    product exists to replace."""
    db, client = env["db"], env["client"]
    start, end = warfarin_span(AI_NOTE)
    client.post(
        "/entries/a-ai/highlights",
        json={"span_start": start, "span_end": end},
        headers=auth(*CLIN_A),
    )

    payload = client.get("/clinic/learning", headers=auth(*CLIN_A)).json()
    tags = {row["feature_tag"]: row for row in payload["weights"]}
    assert "med:warfarin" in tags
    assert tags["med:warfarin"]["positive_signals"] >= 1
    assert tags["med:warfarin"]["direction"] == "promotes"
    assert payload["policy"]["half_life_days"] == learning.SIGNAL_HALF_LIFE_DAYS


def test_the_learning_surface_carries_no_patient_data(env):
    """Tags and counts only — the learning substrate is vocabulary, not prose."""
    db, client = env["db"], env["client"]
    start, end = warfarin_span(AI_NOTE)
    client.post(
        "/entries/a-ai/highlights",
        json={"span_start": start, "span_end": end},
        headers=auth(*CLIN_A),
    )

    body = client.get("/clinic/learning", headers=auth(*CLIN_A)).text
    for leak in ("Amira", "Marcus", "MRN-A-1", "bruising", "atrial fibrillation", "pa1"):
        assert leak not in body


def test_a_patient_cannot_read_the_clinic_learning_surface(env):
    client = env["client"]
    token = create_access_token(
        user_id="u-a-pt", role=Role.PATIENT, clinic_id="clinic-a", patient_id="pa1"
    )
    response = client.get(
        "/clinic/learning", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
