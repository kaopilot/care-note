"""Measuring the learning loop, and the trap in measuring it.

`learning_eval` answers the half of the self-learning capability the build did
not have: not "is there a mitigation for exposure bias" (there is — D-069) but
"how much bias is left after it". Those are different claims and only the second
one is an evaluation.

The most useful test in this file is the last one. The first version of
`learning_eval` ranked highlights by score and took the top N, which sounds like
what a Glance View does and is not: D-084 surfaces protected classes regardless
of rank. Measured that way, `entity:allergy` came back as a tag that never
reaches the card — an alarming, entirely false finding, produced by a metric
that modelled a screen nobody sees. A measurement that does not mirror the
surface it claims to measure will invent whatever it was built to look for.

See DECISIONS.md D-092.
"""

from __future__ import annotations

import json

from app.core.enums import HighlightStatus, Role
from app.models import Highlight
from app.services import learning_eval
from app.services.learning import NEVER_DAMPENED


def _highlight(db, *, patient_id, clinic_id, entry_id, score, tags, breakdown,
               status=HighlightStatus.SUGGESTED):
    row = Highlight(
        entry_id=entry_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        span_start=0,
        span_end=5,
        span_text="span",
        risk_reason="test fixture",
        provenance_pointer=f"entry://{entry_id}",
        status=status,
        score=score,
        score_breakdown=json.dumps(breakdown),
        feature_tags=json.dumps(tags),
        created_by="system",
        created_by_role=Role.SYSTEM,
    )
    db.add(row)
    return row


def test_counterfactual_removes_only_the_learned_term():
    """The comparison is reconstructed from the persisted breakdown, so it
    cannot drift from the score the clinician was actually shown."""
    row = Highlight(
        score_breakdown=json.dumps(
            {"recency": 0.2, "risk": 0.1, "entities": 0.1,
             "open_actions": 0.0, "learned": 0.05, "multiplier": 1.0}
        )
    )
    assert learning_eval.counterfactual_score(row) == 0.4


def test_the_multiplier_still_applies_without_the_learned_term():
    row = Highlight(
        score_breakdown=json.dumps(
            {"recency": 0.2, "risk": 0.2, "entities": 0.0,
             "open_actions": 0.0, "learned": 0.1, "multiplier": 0.5}
        )
    )
    assert learning_eval.counterfactual_score(row) == 0.2


def _chart(db, *, clinic_id="clinic-a", patient_id="patient-a1", learned=0.0):
    """A card's worth of ordinary highlights plus one protected allergy.

    Built explicitly rather than leaning on seed data: the whole point of these
    tests is that the measurement is deterministic, and a fixture whose contents
    drift with the seeder would make a moved number ambiguous.
    """
    from app.models import Entry
    from app.core.enums import EntryType, RiskLevel
    from datetime import datetime, timezone

    entry = Entry(
        id=f"e-{patient_id}", patient_id=patient_id, clinic_id=clinic_id,
        author_role=Role.CLINICIAN, author_id="u-a-clinician",
        timestamp=datetime.now(timezone.utc), type=EntryType.CLINICIAN_SECTION,
        title="t", content="content for the fixture", risk_level=RiskLevel.LOW,
    )
    db.add(entry)

    rows = []
    for index in range(learning_eval.TOP_N + 2):
        rows.append(
            _highlight(
                db, patient_id=patient_id, clinic_id=clinic_id, entry_id=entry.id,
                score=0.5 - index * 0.01,
                tags=[f"med:drug{index}"],
                breakdown={"recency": 0.5 - index * 0.01, "risk": 0.0, "entities": 0.0,
                           "open_actions": 0.0, "learned": 0.0, "multiplier": 1.0},
            )
        )
    # Protected, and deliberately scored below every ordinary row — it reaches
    # the card only via the D-084 exemption.
    rows.append(
        _highlight(
            db, patient_id=patient_id, clinic_id=clinic_id, entry_id=entry.id,
            score=0.05, tags=["entity:allergy"],
            breakdown={"recency": 0.05, "risk": 0.0, "entities": 0.0,
                       "open_actions": 0.0, "learned": 0.0, "multiplier": 1.0},
        )
    )
    db.flush()
    return rows


def test_zero_displacement_when_nothing_was_learned(db_session, seeded):
    """A clinic that has taught the system nothing must measure as unchanged.

    If this reported movement, the metric would be picking up noise from the
    reconstruction rather than the learned term.
    """
    _chart(db_session)
    report = learning_eval.evaluate(db_session, "clinic-a")
    assert report.displacement_rate == 0.0
    assert report.slots_changed == 0


def test_a_learned_weight_that_reorders_the_card_is_measured(db_session, seeded):
    """The metric must move when learning moves the card."""
    rows = _chart(db_session)
    # The weakest ordinary row wins on its learned term alone.
    loser = rows[learning_eval.TOP_N + 1]
    loser.score_breakdown = json.dumps(
        {"recency": 0.05, "risk": 0.0, "entities": 0.0,
         "open_actions": 0.0, "learned": 0.9, "multiplier": 1.0}
    )
    loser.score = 0.95
    db_session.flush()

    report = learning_eval.evaluate(db_session, "clinic-a")
    assert report.slots_changed > 0
    assert report.displacement_rate > 0.0


def test_a_protected_tag_losing_a_slot_is_reported(db_session, seeded):
    """The one result in the report that is a defect rather than a measurement.

    `NEVER_DAMPENED` floors a protected tag's own weight; it cannot stop
    something else being promoted past it. D-084 closed that by exempting
    protected classes from the rank cut — this asserts the evaluator would
    notice if that exemption were ever removed.
    """
    _chart(db_session)
    assert learning_eval.evaluate(db_session, "clinic-a").protected_tags_displaced == []


def test_protected_classes_are_never_reported_blind(db_session, seeded):
    """The regression that made this module worth testing.

    Ranking by score alone reported `entity:allergy` as never reaching the
    card. It does reach it — D-084 surfaces protected classes regardless of
    rank — and the evaluator has to model that or it manufactures the alarm it
    was built to detect. The fixture scores the allergy below every ordinary
    row precisely so a naive top-N implementation fails here.
    """
    _chart(db_session)
    report = learning_eval.evaluate(db_session, "clinic-a")
    blind = set(report.blind_tags)
    assert not (blind & NEVER_DAMPENED), (
        f"a protected class was reported as never surfaced: "
        f"{sorted(blind & NEVER_DAMPENED)}. Either the protected-class "
        "exemption regressed, or the evaluator stopped modelling it."
    )


def test_the_report_is_json_serialisable(db_session, seeded):
    """It is quoted in the brief and printed by a script; both need a dict."""
    payload = learning_eval.evaluate(db_session, "clinic-a").as_dict()
    assert json.loads(json.dumps(payload))["clinic_id"] == "clinic-a"
    for key in ("displacement_rate", "exposure_concentration", "blind_tag_rate"):
        assert 0.0 <= payload[key] <= 1.0


def test_evaluation_is_clinic_scoped(db_session, seeded):
    """One clinic's ranking behaviour must not appear in another's report —
    the same isolation the weights themselves have."""
    _chart(db_session, clinic_id="clinic-a", patient_id="patient-a1")
    _chart(db_session, clinic_id="clinic-b", patient_id="patient-b1")

    a = learning_eval.evaluate(db_session, "clinic-a")
    b = learning_eval.evaluate(db_session, "clinic-b")
    assert a.clinic_id == "clinic-a" and b.clinic_id == "clinic-b"
    assert a.patients_evaluated == 1 and b.patients_evaluated == 1
    assert a.highlights_evaluated == b.highlights_evaluated == learning_eval.TOP_N + 3
