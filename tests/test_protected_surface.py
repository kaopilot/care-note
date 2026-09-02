"""A critical class cannot be ranked, or swiped, off the card.

Scenario 15. The feedback names the failure precisely: a self-learning ranker
only ever sees feedback on what it already surfaced, and a care team under load
dismisses things it should not. "What stops your ranking from learning to bury
an allergy because a tired clinician swiped one away on a Tuesday?"

`NEVER_DAMPENED` (D-041) was the existing answer and it was not sufficient,
because it floors the wrong quantity. It stops learning pushing a protected
tag's own weight below zero. Surfacing is a top-`MAX_HIGHLIGHTS` cut, so two
routes to invisibility stayed open:

* **Relative displacement.** Other tags rising is enough. A clinic that
  interacts heavily with medication changes lifts those scores until the
  allergy falls off the bottom of a six-slot card, never dampened at all.
* **A single dismissal.** Rejected highlights were filtered out of the query,
  so one swipe removed an allergy from the Glance View permanently.

These pin the structural fix: protected classes bypass the ranked cut entirely,
and a dismissed one is demoted rather than deleted.

See DECISIONS.md D-084.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.core.enums import EntryType, HighlightStatus, RiskLevel, Role
from app.models import Entry, Highlight, Patient
from app.services import glance, learning


def _entry(db, patient_id: str, clinic_id: str, content: str) -> Entry:
    entry = Entry(
        patient_id=patient_id,
        clinic_id=clinic_id,
        author_role=Role.CLINICIAN,
        author_id="u-a-clinician",
        timestamp=datetime.now(timezone.utc) - timedelta(days=1),
        type=EntryType.CLINICIAN_SECTION,
        title="note",
        content=content,
        risk_level=RiskLevel.MEDIUM,
        provenance_pointer="entry:x",
        version_number=1,
    )
    db.add(entry)
    db.flush()
    return entry


def _highlight(db, entry, *, tags, score, status=HighlightStatus.SUGGESTED):
    row = Highlight(
        entry_id=entry.id,
        clinic_id=entry.clinic_id,
        patient_id=entry.patient_id,
        span_start=0,
        span_end=min(10, len(entry.content)),
        span_text=entry.content[:10],
        source_version_number=1,
        risk_reason="test fixture",
        provenance_pointer=f"entry:{entry.id}",
        status=status,
        score=score,
        score_breakdown="{}",
        feature_tags=json.dumps(tags),
        created_by="system",
        created_by_role=Role.SYSTEM,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture()
def chart(db_session, seeded):
    """A busy chart: the card is already full of higher-scoring ordinary items."""
    db = db_session
    patient = db.query(Patient).filter(Patient.id == "patient-a1").one()

    for i in range(glance.MAX_HIGHLIGHTS + 3):
        entry = _entry(db, patient.id, patient.clinic_id, f"routine medication review {i}")
        _highlight(db, entry, tags=["med:metformin"], score=0.90 - i * 0.01)

    allergy_entry = _entry(db, patient.id, patient.clinic_id, "allergic to penicillin")
    allergy = _highlight(db, allergy_entry, tags=["entity:allergy"], score=0.05)
    db.commit()
    return {"db": db, "patient": patient, "allergy": allergy}


def _glance(chart):
    return glance.build_glance(
        chart["db"],
        role=Role.CLINICIAN,
        user_id="u-a-clinician",
        patient=chart["patient"],
    )


def test_the_card_is_genuinely_oversubscribed():
    """Guard: the scenario only means anything if the cap actually binds."""
    assert glance.MAX_HIGHLIGHTS < glance.MAX_HIGHLIGHTS + 3


def test_a_lowest_scoring_allergy_still_reaches_the_card(chart):
    """Rank decides order. It does not decide whether an allergy appears."""
    highlights = _glance(chart)["highlights"]
    ids = {h["id"] for h in highlights}
    assert chart["allergy"].id in ids, (
        "an allergy scoring below every other candidate was ranked off the card"
    )


def test_the_allergy_is_marked_as_exempt_not_smuggled_in(chart):
    """An unranked item with no stated reason is its own trust problem."""
    highlights = _glance(chart)["highlights"]
    allergy = next(h for h in highlights if h["id"] == chart["allergy"].id)
    assert allergy["protected"] is True
    assert allergy["protected_reason"], "the card must be able to say why this is here"
    assert "allergy" in allergy["protected_reason"].lower()


def test_dismissing_an_allergy_demotes_it_rather_than_deleting_it(chart):
    """One tired swipe must not make an allergy invisible."""
    chart["allergy"].status = HighlightStatus.REJECTED
    chart["db"].commit()

    highlights = _glance(chart)["highlights"]
    ids = {h["id"] for h in highlights}
    assert chart["allergy"].id in ids, "a dismissed allergy vanished from the card"

    allergy = next(h for h in highlights if h["id"] == chart["allergy"].id)
    assert allergy["status"] == str(HighlightStatus.REJECTED), (
        "it stays visible as dismissed — not silently resurrected as live"
    )
    assert highlights[-1]["id"] == chart["allergy"].id, "demoted to the end, not promoted"


def test_dismissing_an_ordinary_suggestion_does_remove_it(chart):
    """The exemption is narrow. Dismissal still works for everything else."""
    ordinary = (
        chart["db"]
        .query(Highlight)
        .filter(Highlight.feature_tags.like("%metformin%"))
        .order_by(Highlight.score.desc())
        .first()
    )
    ordinary.status = HighlightStatus.REJECTED
    chart["db"].commit()

    ids = {h["id"] for h in _glance(chart)["highlights"]}
    assert ordinary.id not in ids, (
        "protecting allergies must not accidentally protect everything"
    )


def test_protected_classes_are_keyed_to_the_never_dampened_set(chart):
    """One list, not two.

    A second hand-maintained "things that matter" list would drift from
    NEVER_DAMPENED within a phase, and the drift would be silent.
    """
    for tag in learning.NEVER_DAMPENED:
        entry = _entry(chart["db"], chart["patient"].id, chart["patient"].clinic_id, "x")
        row = _highlight(chart["db"], entry, tags=[tag], score=0.0)
        chart["db"].commit()
        ids = {h["id"] for h in _glance(chart)["highlights"]}
        assert row.id in ids, f"{tag} is never-dampened but was ranked off the card"


def test_learning_can_still_order_within_the_protected_set(chart):
    """The floor is on visibility, not on ranking.

    Learning is still allowed to do its job — it just cannot do it by making a
    critical finding disappear.
    """
    second = _entry(chart["db"], chart["patient"].id, chart["patient"].clinic_id, "anaphylaxis")
    high = _highlight(chart["db"], second, tags=["symptom:anaphylaxis"], score=0.99)
    chart["db"].commit()

    highlights = [h for h in _glance(chart)["highlights"] if h["protected"]]
    assert highlights[0]["id"] == high.id, "higher-scoring protected item should sort first"
    assert {h["id"] for h in highlights} >= {high.id, chart["allergy"].id}
