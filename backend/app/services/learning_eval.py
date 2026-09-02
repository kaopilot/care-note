"""Measuring what the learning loop did to the ranking.

Why this exists
---------------
The capability list asks for self-learning that is "clinic-scoped, bounded,
auditable, resistant to alert fatigue, **and evaluated for exposure bias**". The
build had four of those five. Exposure bias had a *mitigation* — one reserved
suggestion slot (D-069) — and three tests asserting that the mitigation exists,
is deterministic, and cannot surface a meaningless span.

None of that is an evaluation. "We built a mechanism" and "we measured what the
mechanism left behind" are different claims, and marking the row SURVIVES on the
strength of the first was an overclaim (D-091). This module is the second claim.

What it measures
----------------
The counterfactual is cheap because the ranking is additive and the breakdown is
persisted: every `Highlight` stores `{recency, risk, entities, open_actions,
learned, multiplier}`. Removing the learned term and re-totalling gives exactly
the ranking this clinic would have had if it had never taught the system
anything. Nothing is re-scored, no model runs, and the result is deterministic.

Four numbers, each answering a question a reviewer would actually ask:

* **Displacement** — how much did learning change what a clinician sees? If it
  is zero the loop is decoration; if it is total the rules stopped mattering.
* **Suppression** — which tags did learning push *down*, and did any protected
  class move at all? `NEVER_DAMPENED` floors the weight, but a floored tag can
  still lose its slot to a promoted one, which the floor alone does not catch.
* **Exposure concentration** — what share of surfaced slots go to tags this
  clinic has already given feedback on? This is the bias itself, stated as a
  number. A loop that has closed on its own history reads near 1.0.
* **Blind tags** — tags present in the record that have never once been
  surfaced, so the loop has never had the chance to learn they matter. This is
  the population the exploration slot is drawing from, and its size is the
  honest measure of how far from closed the loop is.

What it is not
--------------
Not off-policy evaluation. The counterfactual here is "same candidates, no
learned term", which measures the *re-ranking* effect. It cannot measure what
the rules never generated in the first place — for that you need held-out charts
with known-correct highlights, which means labelled clinical data this build
does not have and could not synthesise honestly. That limit is the finding, not
an omission: it is why the exposure-bias row is PARTIAL and not SURVIVES.

See DECISIONS.md D-092.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.enums import HighlightStatus
from app.models import Highlight, InteractionLog
from app.services.glance import MAX_HIGHLIGHTS
from app.services.learning import NEVER_DAMPENED
from app.services.scoring import decode_breakdown

# The evaluation window is the Glance View's own cap, imported rather than
# restated. A measurement of "what the clinician sees" that used a different
# number than the card does would be measuring a surface nobody looks at, and
# would silently stop matching the day the cap is tuned.
TOP_N = MAX_HIGHLIGHTS


@dataclass
class TagMovement:
    tag: str
    mean_delta: float
    occurrences: int
    protected: bool

    @property
    def direction(self) -> str:
        if self.mean_delta > 0.0005:
            return "promoted"
        if self.mean_delta < -0.0005:
            return "suppressed"
        return "unchanged"


@dataclass
class ExposureReport:
    clinic_id: str
    patients_evaluated: int
    highlights_evaluated: int

    # Displacement
    slots_changed: int = 0
    slots_total: int = 0
    patients_with_changed_top: int = 0

    # Suppression
    movements: list[TagMovement] = field(default_factory=list)
    protected_tags_displaced: list[str] = field(default_factory=list)

    # Exposure
    surfaced_slots_with_prior_feedback: int = 0
    blind_tags: list[str] = field(default_factory=list)
    tags_with_feedback: int = 0
    tags_seen: int = 0

    @property
    def displacement_rate(self) -> float:
        """Share of visible slots learning changed. 0.0 = decoration."""
        return self.slots_changed / self.slots_total if self.slots_total else 0.0

    @property
    def exposure_concentration(self) -> float:
        """Share of visible slots held by already-fed-back tags.

        Near 1.0 means the loop is only ever hearing about what it already
        believed. This is the bias, as a number.
        """
        return (
            self.surfaced_slots_with_prior_feedback / self.slots_total
            if self.slots_total
            else 0.0
        )

    @property
    def blind_tag_rate(self) -> float:
        """Share of tags in the record the loop has never surfaced."""
        return len(self.blind_tags) / self.tags_seen if self.tags_seen else 0.0

    def as_dict(self) -> dict:
        return {
            "clinic_id": self.clinic_id,
            "patients_evaluated": self.patients_evaluated,
            "highlights_evaluated": self.highlights_evaluated,
            "displacement_rate": round(self.displacement_rate, 4),
            "slots_changed": self.slots_changed,
            "slots_total": self.slots_total,
            "patients_with_changed_top": self.patients_with_changed_top,
            "exposure_concentration": round(self.exposure_concentration, 4),
            "blind_tag_rate": round(self.blind_tag_rate, 4),
            "blind_tags": sorted(self.blind_tags),
            "tags_with_feedback": self.tags_with_feedback,
            "tags_seen": self.tags_seen,
            "protected_tags_displaced": sorted(self.protected_tags_displaced),
            "suppressed": [
                {"tag": m.tag, "mean_delta": round(m.mean_delta, 4), "n": m.occurrences}
                for m in self.movements
                if m.direction == "suppressed"
            ],
            "promoted": [
                {"tag": m.tag, "mean_delta": round(m.mean_delta, 4), "n": m.occurrences}
                for m in self.movements
                if m.direction == "promoted"
            ],
        }


def _tags(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def counterfactual_score(highlight: Highlight) -> float:
    """What this highlight would have scored with no learned term.

    Reconstructed from the persisted breakdown rather than re-scored, so the
    comparison cannot drift from what the clinician was actually shown.
    """
    breakdown = decode_breakdown(highlight.score_breakdown)
    multiplier = breakdown.get("multiplier", 1.0)
    base = sum(
        value
        for key, value in breakdown.items()
        if key not in {"multiplier", "learned"}
    )
    return round(base * multiplier, 4)


def _is_protected(tags: list[str]) -> bool:
    return any(tag in NEVER_DAMPENED for tag in tags)


def _visible(rows: list[Highlight], *, key, top_n: int) -> list[str]:
    """The ids a clinician would actually see, under a given ranking.

    This has to mirror `glance._top_highlights`, not approximate it. An earlier
    version of this module ranked purely by score and took the top N — and
    reported `entity:allergy` as never reaching the card, which is false. D-084
    surfaces protected classes *regardless of rank*, precisely so that ranking
    cannot decide whether an allergy appears. A measurement that ignores the
    exemption measures a screen nobody sees, and would have manufactured exactly
    the alarming finding it was built to look for.
    """
    protected = [h for h in rows if _is_protected(_tags(h.feature_tags))]
    ordinary = [h for h in rows if not _is_protected(_tags(h.feature_tags))]
    ranked = sorted(ordinary, key=key, reverse=True)[:top_n]
    return [h.id for h in protected] + [h.id for h in ranked]


def evaluate(db: Session, clinic_id: str, *, top_n: int = TOP_N) -> ExposureReport:
    """Rank this clinic's highlights with and without the learned term."""
    highlights = (
        db.query(Highlight)
        .filter(Highlight.clinic_id == clinic_id)
        .filter(Highlight.status != HighlightStatus.REJECTED)
        .all()
    )

    # Tags this clinic has ever given feedback on. Accept/reject decisions plus
    # anything the interaction log recorded — the same evidence the weights are
    # built from, so the exposure figure and the weights agree about history.
    fed_back: set[str] = set()
    for row in db.query(Highlight).filter(Highlight.clinic_id == clinic_id).all():
        if row.status in (HighlightStatus.ACCEPTED, HighlightStatus.REJECTED):
            fed_back.update(_tags(row.feature_tags))
    for log in db.query(InteractionLog).filter(InteractionLog.clinic_id == clinic_id):
        fed_back.update(_tags(log.content_features))

    by_patient: dict[str, list[Highlight]] = {}
    for row in highlights:
        by_patient.setdefault(row.patient_id, []).append(row)

    report = ExposureReport(
        clinic_id=clinic_id,
        patients_evaluated=len(by_patient),
        highlights_evaluated=len(highlights),
    )

    deltas: dict[str, list[float]] = {}
    all_tags: set[str] = set()
    surfaced_tags: set[str] = set()

    for rows in by_patient.values():
        learned_top = _visible(rows, key=lambda h: h.score, top_n=top_n)
        naive_top = _visible(rows, key=counterfactual_score, top_n=top_n)

        report.slots_total += len(learned_top)
        changed = len(set(learned_top) ^ set(naive_top)) // 2
        report.slots_changed += changed
        if changed:
            report.patients_with_changed_top += 1

        # A protected tag that was visible without learning and is not visible
        # with it. The NEVER_DAMPENED floor stops a tag's own weight going
        # negative; it does not stop something else being promoted past it.
        lost = set(naive_top) - set(learned_top)
        for row in rows:
            row_tags = _tags(row.feature_tags)
            all_tags.update(row_tags)
            if row.id in learned_top:
                surfaced_tags.update(row_tags)
                if any(tag in fed_back for tag in row_tags):
                    report.surfaced_slots_with_prior_feedback += 1
            if row.id in lost:
                for tag in row_tags:
                    if tag in NEVER_DAMPENED:
                        report.protected_tags_displaced.append(tag)

            breakdown = decode_breakdown(row.score_breakdown)
            learned_term = breakdown.get("learned", 0.0)
            for tag in row_tags:
                deltas.setdefault(tag, []).append(learned_term)

    report.movements = sorted(
        (
            TagMovement(
                tag=tag,
                mean_delta=sum(values) / len(values),
                occurrences=len(values),
                protected=tag in NEVER_DAMPENED,
            )
            for tag, values in deltas.items()
        ),
        key=lambda m: m.mean_delta,
    )
    report.tags_seen = len(all_tags)
    report.tags_with_feedback = len(all_tags & fed_back)
    # Present in the record, never once surfaced into a visible slot. The loop
    # has had no opportunity to learn these matter — the population the
    # exploration slot exists to draw from.
    report.blind_tags = sorted(all_tags - surfaced_tags)
    return report
