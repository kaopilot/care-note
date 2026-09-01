"""One clinical disagreement is one card, however many entries evidence it.

Scenarios 13 and 15. Detection is pairwise, which is right for a primitive that
has to be individually checkable and individually citable, and wrong as a
display unit.

The defect this pins was found by probing rather than by a test, because every
existing test used exactly one assertion and one denial — the one shape where
pairwise and grouped output are identical. A real chart does not look like that.
A penicillin allergy gets re-recorded at every visit, so four routine mentions
against two denials produce eight pairs that all say the same clinical thing.

That mattered more than duplication. The Glance View caps the list at
`MAX_CONTRADICTIONS`, so the copies filled the cap and an unrelated metformin
dose disagreement was evicted from the card entirely — a real, unresolved
disagreement made invisible by a different one being mentioned often. The
failure got worse the longer the record grew, which is precisely the case a
longitudinal product exists to serve.

See DECISIONS.md D-081.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.enums import RiskLevel
from app.services import contradictions


class _Entry:
    def __init__(self, entry_id: str, entry_type: str, content: str, role: str) -> None:
        self.id = entry_id
        self.type = entry_type
        self.content = content
        self.author_role = role
        self.timestamp = datetime.now(timezone.utc)
        self.title = ""
        self.version_number = 1


def _allergy_recorded(n: int) -> _Entry:
    return _Entry(
        f"e-allergy-{n}",
        "staff_note",
        f"Visit {n}: patient reports allergy to penicillin.",
        "staff",
    )


def _allergy_denied(n: int) -> _Entry:
    return _Entry(
        f"e-denial-{n}",
        "clinician_section",
        "Patient states she has no known drug allergies.",
        "clinician",
    )


def _dose_high() -> _Entry:
    return _Entry("e-dose-1", "clinician_section", "Continue metformin 1g BD.", "clinician")


def _dose_low() -> _Entry:
    return _Entry("e-dose-2", "staff_note", "Confirmed metformin 500mg BD.", "staff")


def test_repeated_mentions_collapse_to_one_disagreement():
    """Four assertions against two denials is one clinical problem, not eight."""
    entries = [_allergy_recorded(i) for i in range(4)] + [_allergy_denied(i) for i in range(2)]

    pairs = contradictions.detect(entries)
    grouped = contradictions.group(pairs)

    assert len(pairs) > len(grouped), "the fan-out this guards against must exist"
    subjects = [(g.kind, g.subject) for g in grouped]
    assert subjects.count(("assertion_vs_denial", "penicillin")) == 1
    assert len(grouped) == 1


def test_grouping_keeps_every_citation():
    """Collapsing the card must not collapse the provenance.

    Scenario 16 requires every finding stay addressable. A grouped card that
    said "and 7 others" without pointers would trade one failure for another.
    """
    entries = [_allergy_recorded(i) for i in range(4)] + [_allergy_denied(i) for i in range(2)]
    grouped = contradictions.group(contradictions.detect(entries))
    finding = grouped[0]

    cited = (
        {finding.left.left_entry_id, finding.left.right_entry_id}
        | {row[0] for row in finding.also_left}
        | {row[0] for row in finding.also_right}
    )
    assert cited == {e.id for e in entries}, "every entry that evidenced it stays reachable"
    assert finding.entry_count == len(entries)
    assert all(row[1] for row in finding.also_left), "each carries its own pointer"
    assert all(row[1] for row in finding.also_right)


def test_a_frequently_mentioned_allergy_cannot_evict_a_dose_disagreement():
    """The regression that made this worth fixing.

    Before grouping, the capped Glance View list filled with copies of the
    penicillin disagreement and the metformin dose conflict disappeared.
    """
    entries = (
        [_allergy_recorded(i) for i in range(4)]
        + [_allergy_denied(i) for i in range(2)]
        + [_dose_high(), _dose_low()]
    )
    grouped = contradictions.group(contradictions.detect(entries))

    from app.services.glance import MAX_CONTRADICTIONS

    shown = grouped[:MAX_CONTRADICTIONS]
    kinds = {g.kind for g in shown}
    assert "dose_disagreement" in kinds, "a distinct disagreement must survive the cap"
    assert "assertion_vs_denial" in kinds
    assert len(shown) <= MAX_CONTRADICTIONS


def test_two_different_allergens_stay_two_findings():
    """Grouping is by subject, so it must not merge unrelated disagreements."""
    entries = [
        _allergy_recorded(0),
        _Entry("e-a2", "staff_note", "Patient reports allergy to aspirin.", "staff"),
        _allergy_denied(0),
    ]
    grouped = contradictions.group(contradictions.detect(entries))
    subjects = {g.subject for g in grouped}
    assert {"penicillin", "aspirin"} <= subjects


def test_group_reports_human_human_if_any_pair_is():
    """The pessimistic reading is the honest one.

    If even one pair pits two people against each other, no precedence rule
    settles the disagreement, and the card should say so for the whole group.
    """
    ai_note = _Entry(
        "e-ai",
        "ai_patient_session_summary",
        "Patient reports allergy to penicillin.",
        "system",
    )
    entries = [ai_note, _allergy_recorded(1), _allergy_denied(0)]
    grouped = contradictions.group(contradictions.detect(entries))
    finding = next(g for g in grouped if g.subject == "penicillin")
    assert finding.human_human is True


def test_severity_survives_grouping():
    """An allergy disagreement must not be softened by being grouped."""
    entries = [_allergy_recorded(i) for i in range(3)] + [_allergy_denied(0)]
    grouped = contradictions.group(contradictions.detect(entries))
    assert grouped[0].severity in {RiskLevel.CRITICAL, RiskLevel.HIGH}
    assert grouped[0].pair_count == 3
