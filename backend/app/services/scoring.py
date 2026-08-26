"""Importance scoring for the Glance View.

Phase 2 is rule-based only. The brief's four factors are each a named term with
a fixed weight, and every score is returned with its breakdown attached so the
UI can answer "why is this at the top?" without guessing.

    score = W_RECENCY  * recency(timestamp)
          + W_RISK     * risk(risk_level)
          + W_ENTITY   * entities(tags)
          + W_ACTION   * unresolved(tags, tasks)
          + W_LEARNED  * learned(clinic, tags)      <- Phase 4 fills this in

Why the learned term is already here, returning zero
----------------------------------------------------
`learned_component()` reads `FeatureWeight`, which is empty until Phase 4 starts
writing to it. So today it contributes exactly 0.0 and the ranking is purely
rule-based — but the *shape* of the score, the breakdown keys, the storage
format and the UI that renders them are all final. Phase 4 becomes "start
writing rows", not "restructure scoring and every view that reads it".

Deliberately not a model. A weighted sum over named features is inspectable: a
clinician can be shown the arithmetic behind a suggestion. In a system whose
stated purpose is calibrating trust in machine output, a ranker nobody can
explain would undercut the product it is serving (DECISIONS.md D-029).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.enums import DecayState, RiskLevel
from app.models import FeatureWeight

# Term weights. Chosen so that no single factor can dominate: a critical risk
# tag on a two-month-old entry should still lose to a recent unresolved action
# on a high-risk one, which is how a clinician actually triages a chart.
W_RECENCY = 0.30
W_RISK = 0.30
W_ENTITY = 0.20
W_ACTION = 0.20
W_LEARNED = 0.25  # additive on top; can lift a candidate but never invent one

# Recency decays with a 10-day half-life: yesterday's note is worth roughly
# double a note from a week and a half ago, and a year-old note is ~0.
RECENCY_HALF_LIFE_DAYS = 10.0

RISK_WEIGHTS: dict[str, float] = {
    str(RiskLevel.NONE): 0.0,
    str(RiskLevel.LOW): 0.25,
    str(RiskLevel.MEDIUM): 0.55,
    str(RiskLevel.HIGH): 0.85,
    str(RiskLevel.CRITICAL): 1.0,
}

# Entity tags that count toward the "tagged clinical entities" term, and how
# much each is worth relative to the others.
ENTITY_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("entity:allergy", 1.0),
    ("symptom:", 0.8),
    ("finding:", 0.7),
    ("med:", 0.6),
    ("entity:chief_complaint", 0.5),
)

# Decay states down-weight rather than hide. A cold entry that is still the only
# record of an allergy must remain reachable — see DECISIONS.md D-009.
DECAY_MULTIPLIER: dict[str, float] = {
    str(DecayState.HOT): 1.0,
    str(DecayState.WARM): 0.7,
    str(DecayState.COLD): 0.4,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat them as UTC rather than
    crashing on a subtraction, and rather than silently shifting by local
    offset."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def recency_score(timestamp: datetime, *, now: datetime | None = None) -> float:
    age_days = max(0.0, ((now or _now()) - _aware(timestamp)).total_seconds() / 86400.0)
    return math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)


def risk_score(risk_level: str | RiskLevel) -> float:
    return RISK_WEIGHTS.get(str(risk_level), 0.0)


def entity_score(tags: list[str]) -> float:
    """Best-matching entity class wins, with a small bonus for breadth.

    Summing every tag would let a sentence listing six medications outrank a
    single documented anaphylaxis, which is the wrong triage.
    """
    best = 0.0
    matched = 0
    for tag in tags:
        for prefix, weight in ENTITY_WEIGHTS:
            if tag == prefix or tag.startswith(prefix):
                best = max(best, weight)
                matched += 1
                break
    if not best:
        return 0.0
    return min(1.0, best + 0.05 * (matched - 1))


def action_score(tags: list[str], *, open_task_count: int = 0) -> float:
    score = 0.6 if "entity:open_action" in tags else 0.0
    if open_task_count:
        score = max(score, min(1.0, 0.7 + 0.15 * open_task_count))
    return score


def learned_component(db: Session, clinic_id: str, tags: list[str]) -> float:
    """Learned per-tag weight, clinic-scoped. Returns 0.0 until Phase 4 writes.

    Clinic scoping is not incidental: one clinic's attention habits must never
    influence another's prioritisation, for the same reason their notes are
    isolated. The unique constraint on `(clinic_id, feature_tag)` is what makes
    that structural rather than a filter someone could forget.
    """
    if not tags:
        return 0.0
    rows = (
        db.query(FeatureWeight)
        .filter(FeatureWeight.clinic_id == clinic_id)
        .filter(FeatureWeight.feature_tag.in_(tags))
        .all()
    )
    if not rows:
        return 0.0
    # Mean rather than sum: a span carrying eight learned tags should not
    # outrank one carrying the single tag this clinic cares most about.
    return sum(row.weight for row in rows) / len(rows)


def score_span(
    db: Session,
    *,
    clinic_id: str,
    timestamp: datetime,
    risk_level: str | RiskLevel,
    tags: list[str],
    open_task_count: int = 0,
    decay_state: str | DecayState = DecayState.HOT,
    now: datetime | None = None,
) -> tuple[float, dict[str, float]]:
    """Score one highlight candidate. Returns `(score, breakdown)`.

    The breakdown is persisted on the Highlight and rendered in the UI. It is
    the difference between "the system says this matters" and "the system says
    this matters because it is 2 days old, tagged high risk, mentions warfarin,
    and has an unresolved action".
    """
    recency = recency_score(timestamp, now=now)
    risk = risk_score(risk_level)
    entity = entity_score(tags)
    action = action_score(tags, open_task_count=open_task_count)
    learned = learned_component(db, clinic_id, tags)

    breakdown = {
        "recency": round(W_RECENCY * recency, 4),
        "risk": round(W_RISK * risk, 4),
        "entities": round(W_ENTITY * entity, 4),
        "open_actions": round(W_ACTION * action, 4),
        "learned": round(W_LEARNED * learned, 4),
    }
    # Hedged language is a discount, not a term: speculation should rank below
    # an equivalent finding, but should not be pushed off the card entirely.
    multiplier = DECAY_MULTIPLIER.get(str(decay_state), 1.0)
    if "quality:hedged" in tags:
        multiplier *= 0.85

    total = round(sum(breakdown.values()) * multiplier, 4)
    breakdown["multiplier"] = round(multiplier, 4)
    return total, breakdown


def encode_breakdown(breakdown: dict[str, float]) -> str:
    return json.dumps(breakdown, sort_keys=True)


def decode_breakdown(raw: str | None) -> dict[str, float]:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
