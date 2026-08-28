"""Self-learning importance — turning clinician behaviour into ranking weight.

Phase 2 wrote `InteractionLog` rows and nothing read them. This module is the
first reader, and it closes the loop the schema was built for:

    InteractionLog (tags of what a human touched)
        -> weighted, time-decayed evidence per tag
        -> FeatureWeight (clinic, tag) -> weight in (-1, 1)
        -> scoring.learned_component()  -> Glance View ranking

Five decisions shape this file, and each is a constraint on what learning is
*allowed* to do rather than a technique for making it stronger.

**The log is the source of truth; FeatureWeight is a materialised view.**
`recompute_tags()` and `rebuild_clinic()` are the only writers, and they run the
same accumulation. There is no separate "online update" formula that could drift
from the batch one — an incremental write recomputes the affected tags from the
log rather than nudging a stored number. That costs a small scan on write paths
(never on the Glance View read path) and buys something worth more: the weights
can always be rebuilt from scratch and must come out identical, which is
`test_self_learning_importance.py`'s idempotence assertion.

**Evidence decays.** A signal from last year counts less than one from
yesterday (`SIGNAL_HALF_LIFE_DAYS`). Without this, "learning" is really just
accumulation: a clinic that cared about something in 2024 could never stop
caring, and the system would keep surfacing what a previous cohort of
clinicians found interesting. Adaptive has to include the ability to forget.

**Weights saturate.** `evidence / (|evidence| + SATURATION)` is bounded in
(-1, 1), so no amount of repetition lets one tag dominate the score, and the
learned term is capped at `W_LEARNED` of the total. Learning re-ranks the
things rules already surfaced; it never becomes the ranking.

**Learning cannot invent a highlight, only move one.** A span with no clinical
reason produces no highlight at all (`highlights.refresh_entry_highlights`,
rule 1), and that check runs before scoring. So the worst a runaway weight can
do is reorder candidates a rule already found worth showing.

**Some content is never dampened.** `NEVER_DAMPENED` tags are floored at zero.
A clinician dismissing three warfarin suggestions should teach the system to
stop nagging about warfarin. A clinician dismissing three anaphylaxis
suggestions must never teach it to stop mentioning anaphylaxis. That asymmetry
is deliberate: the cost of a missed allergy is not symmetric with the cost of
one extra line on a card, so the learning rule is not symmetric either
(D-041).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.enums import InteractionAction, Role
from app.core.timeutil import iso_utc
from app.models import FeatureWeight, InteractionLog

# --------------------------------------------------------------------------
# Policy constants
# --------------------------------------------------------------------------

# How much each kind of interaction counts as evidence that this content
# matters. Ordered by how deliberate the act is: marking a phrase by hand is an
# unambiguous statement about importance; editing a note you were already in is
# much weaker evidence about the *subject matter*.
ACTION_SIGNAL: dict[str, float] = {
    str(InteractionAction.MANUAL_HIGHLIGHT): 1.0,
    str(InteractionAction.PIN): 1.0,
    str(InteractionAction.ACCEPT_HIGHLIGHT): 0.8,
    str(InteractionAction.REJECT_HIGHLIGHT): -0.8,
    str(InteractionAction.COMMENT): 0.4,
    str(InteractionAction.EDIT): 0.3,
    str(InteractionAction.RESOLVE_COMMENT): 0.1,
    # Recorded, deliberately not learned from. See InteractionAction docstring.
    str(InteractionAction.CREATE): 0.0,
    str(InteractionAction.VIEW): 0.0,
}

# Whose behaviour trains the ranking. The brief names clinicians and staff.
# Admin is excluded for the same reason it cannot author clinical content
# (D-011): it is an oversight role, and oversight activity is not clinical
# attention. Patients are excluded because the surface being trained is the
# clinician Glance View — a patient's own reading habits should not decide what
# a doctor sees first.
LEARNING_ROLES: frozenset[str] = frozenset({str(Role.CLINICIAN), str(Role.STAFF)})

# Evidence half-life. Ninety days is roughly a clinical quarter: long enough
# that a habit has to be sustained to move the ranking, short enough that the
# system tracks a rotating cohort of clinicians rather than a historical one.
SIGNAL_HALF_LIFE_DAYS = 90.0

# Saturation constant for the squashing function. At 2.5, one deliberate manual
# highlight moves a tag to ~0.29 and three move it to ~0.55 — a visible nudge
# from a single action, diminishing returns after that.
SATURATION = 2.5

# Tags whose learned weight is floored at zero: behaviour can promote them,
# never suppress them.
NEVER_DAMPENED: frozenset[str] = frozenset(
    {
        "entity:allergy",
        "risk:critical",
        "symptom:anaphylaxis",
        "symptom:suicidal",
        "symptom:self-harm",
        "symptom:sepsis",
    }
)

# Tags describing the container rather than the clinical content. They are
# recorded on interactions and are useful for auditing what the clinic touches,
# but they are not learnable: `type:staff_note` appears on every staff note, so
# weighting it would drift into "staff notes matter" — a statement about
# authorship, not about the patient. Learning stays on clinical vocabulary.
NON_LEARNABLE_PREFIXES: tuple[str, ...] = ("type:", "source:", "signal:")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return _now()
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_learnable(tag: str) -> bool:
    """Whether a tag is eligible to carry a learned weight at all."""
    return bool(tag) and not tag.startswith(NON_LEARNABLE_PREFIXES)


def squash(evidence: float) -> float:
    """Bounded, monotonic, diminishing-returns map from evidence to weight."""
    return evidence / (abs(evidence) + SATURATION)


# --------------------------------------------------------------------------
# Accumulation
# --------------------------------------------------------------------------


@dataclass
class TagEvidence:
    """What the log says about one tag, before squashing."""

    evidence: float = 0.0
    positive: int = 0
    negative: int = 0
    contributing_actions: dict[str, int] = field(default_factory=dict)

    def add(self, action: str, weighted: float) -> None:
        self.evidence += weighted
        if weighted > 0:
            self.positive += 1
        elif weighted < 0:
            self.negative += 1
        self.contributing_actions[action] = self.contributing_actions.get(action, 0) + 1


def _decode(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _accumulate(
    rows: list[InteractionLog],
    *,
    now: datetime,
    only_tags: set[str] | None = None,
) -> dict[str, TagEvidence]:
    """Fold interaction rows into per-tag evidence.

    One implementation, used by both the incremental and the full-rebuild path,
    so the two cannot disagree about what a weight means.
    """
    out: dict[str, TagEvidence] = defaultdict(TagEvidence)
    for row in rows:
        base = ACTION_SIGNAL.get(str(row.action), 0.0)
        if base == 0.0:
            continue
        age_days = max(0.0, (now - _aware(row.timestamp)).total_seconds() / 86400.0)
        weighted = base * math.pow(0.5, age_days / SIGNAL_HALF_LIFE_DAYS)
        for tag in set(_decode(row.content_features)):
            if not is_learnable(tag):
                continue
            if only_tags is not None and tag not in only_tags:
                continue
            out[tag].add(str(row.action), weighted)
    return dict(out)


def _weight_for(tag: str, evidence: float) -> float:
    weight = squash(evidence)
    if tag in NEVER_DAMPENED:
        return max(0.0, weight)
    return round(weight, 6)


def _upsert(
    db: Session, clinic_id: str, tag: str, evidence: TagEvidence
) -> FeatureWeight:
    row = (
        db.query(FeatureWeight)
        .filter(FeatureWeight.clinic_id == clinic_id, FeatureWeight.feature_tag == tag)
        .first()
    )
    if row is None:
        row = FeatureWeight(clinic_id=clinic_id, feature_tag=tag)
        db.add(row)
    row.weight = _weight_for(tag, evidence.evidence)
    row.positive_signals = evidence.positive
    row.negative_signals = evidence.negative
    return row


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def recompute_tags(
    db: Session, clinic_id: str, tags: list[str], *, now: datetime | None = None
) -> dict[str, float]:
    """Recompute the weights of specific tags from the log. Returns {tag: weight}.

    Called on every write path that records a signal, so a clinician sees the
    effect of confirming a highlight on the next render rather than after a
    nightly batch. Only the tags just touched are rescanned.

    The SQL `LIKE` below is a *prefilter*, not the decision: tags are stored as
    JSON string members so `%"med:warfarin"%` matches them, but `_` is a LIKE
    wildcard and several tags contain one. Every candidate row is therefore
    decoded and matched exactly in `_accumulate`. The cost of that is one
    unindexed scan per write; the alternative — a normalised tag join table —
    is the right answer at real volume and is noted in SCHEMA.md rather than
    built here.
    """
    wanted = {tag for tag in dict.fromkeys(tags or []) if is_learnable(tag)}
    if not wanted:
        return {}

    clauses = [InteractionLog.content_features.like(f'%"{tag}"%') for tag in wanted]
    rows = (
        db.query(InteractionLog)
        .filter(InteractionLog.clinic_id == clinic_id)
        .filter(InteractionLog.user_role.in_(list(LEARNING_ROLES)))
        .filter(or_(*clauses))
        .all()
    )

    evidence = _accumulate(rows, now=now or _now(), only_tags=wanted)
    result: dict[str, float] = {}
    for tag in wanted:
        tag_evidence = evidence.get(tag)
        if tag_evidence is None:
            # No learning-eligible evidence exists for this tag. Writing a 0.0
            # row would put it on the transparency surface as something the
            # clinic has an opinion about, which is the opposite of true — and
            # would make `rebuild_clinic`, which deletes such rows, disagree
            # with this path about what the table should contain.
            stale = (
                db.query(FeatureWeight)
                .filter(
                    FeatureWeight.clinic_id == clinic_id,
                    FeatureWeight.feature_tag == tag,
                )
                .first()
            )
            if stale is not None:
                db.delete(stale)
            continue
        result[tag] = _upsert(db, clinic_id, tag, tag_evidence).weight
    db.flush()
    return result


def rebuild_clinic(
    db: Session, clinic_id: str, *, now: datetime | None = None
) -> dict[str, float]:
    """Recompute every weight for one clinic from the whole log.

    Must produce exactly what the incremental path produced — that equality is
    what makes `FeatureWeight` a cache rather than a second source of truth, and
    it is asserted directly in the Phase 4 test.
    """
    rows = (
        db.query(InteractionLog)
        .filter(InteractionLog.clinic_id == clinic_id)
        .filter(InteractionLog.user_role.in_(list(LEARNING_ROLES)))
        .all()
    )
    evidence = _accumulate(rows, now=now or _now())

    existing = {
        row.feature_tag: row
        for row in db.query(FeatureWeight).filter(FeatureWeight.clinic_id == clinic_id).all()
    }
    result: dict[str, float] = {}
    for tag, tag_evidence in evidence.items():
        row = _upsert(db, clinic_id, tag, tag_evidence)
        result[tag] = row.weight

    # A tag whose evidence has fully decayed away, or whose only signals came
    # from an action later reclassified as non-learning, must not keep a stale
    # weight sitting in the table.
    for tag, row in existing.items():
        if tag not in evidence:
            db.delete(row)
    db.flush()
    return result


def apply_signal(
    db: Session,
    *,
    clinic_id: str,
    user_role: Role | str,
    tags: list[str] | None,
    now: datetime | None = None,
) -> dict[str, float]:
    """Update learned weights after one interaction was recorded.

    Deliberately called from `interactions.record_interaction` rather than from
    each route, so it is structurally impossible to record a behavioural signal
    without the learning table seeing it. Same reasoning as the redaction
    chokepoint: a rule enforced in one place is a rule; a rule repeated at six
    call sites is a convention waiting to be forgotten.
    """
    if str(user_role) not in LEARNING_ROLES:
        return {}
    return recompute_tags(db, clinic_id, list(tags or []), now=now)


# --------------------------------------------------------------------------
# Reading — the transparency surface
# --------------------------------------------------------------------------


def top_weights(
    db: Session, clinic_id: str, *, limit: int = 12
) -> list[dict[str, object]]:
    """What this clinic has taught the ranking, most influential first.

    Exposed to clinicians and staff through `GET /clinic/learning`. A system
    that adapts to you and will not tell you how is exactly the kind of opaque
    machine judgement this product exists to replace — so the learned state is
    readable, and every row shows the evidence count behind it, not just a
    number.
    """
    rows = (
        db.query(FeatureWeight)
        .filter(FeatureWeight.clinic_id == clinic_id)
        .all()
    )
    rows.sort(key=lambda row: abs(row.weight), reverse=True)
    return [
        {
            "feature_tag": row.feature_tag,
            "weight": round(row.weight, 4),
            "positive_signals": row.positive_signals,
            "negative_signals": row.negative_signals,
            "direction": (
                "promotes" if row.weight > 0 else "dampens" if row.weight < 0 else "neutral"
            ),
            "floored": row.feature_tag in NEVER_DAMPENED,
            "updated_at": iso_utc(row.updated_at),
        }
        for row in rows[:limit]
    ]


def signal_summary(db: Session, clinic_id: str) -> dict[str, int]:
    """Counts of learning-eligible interactions by action, for the same surface."""
    rows = (
        db.query(InteractionLog)
        .filter(InteractionLog.clinic_id == clinic_id)
        .filter(InteractionLog.user_role.in_(list(LEARNING_ROLES)))
        .all()
    )
    counts: dict[str, int] = {}
    for row in rows:
        if ACTION_SIGNAL.get(str(row.action), 0.0) == 0.0:
            continue
        counts[str(row.action)] = counts.get(str(row.action), 0) + 1
    return counts
