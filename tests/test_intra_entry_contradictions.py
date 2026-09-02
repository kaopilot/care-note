"""Contradictions that happen inside a single entry.

The pairwise detector compared entries against each other and never an entry
against itself. For a chart of typed notes that is the right call — two
sentences in one note were written by one person in one sitting, and flagging
them against each other is mostly noise.

It stops being the right call the moment an entry is a **transcript**.
`run_scribe` writes one Entry per consult session, so a twenty-minute
conversation collapses into one row, and every disagreement that happened
*during* the consult became structurally invisible — including the pairing
scenario 7 is about: an allergy stated at minute two against a drug prescribed
at minute nineteen.

These tests pin both halves: the cases that must now fire, and the cases that
must stay quiet. The second set is the more important one. A detector that
flags ordinary clinical deliberation is worse than one with gaps, because it
teaches people to dismiss the flag that matters (D-068, D-083).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.enums import Role
from app.services import contradictions


class _Entry:
    """Minimal stand-in — `detect` reads id, type, content and author_role."""

    def __init__(self, entry_id: str, content: str, *, ai: bool = True) -> None:
        self.id = entry_id
        self.type = "ai_doctor_consult_summary" if ai else "staff_note"
        self.content = content
        self.author_role = Role.SYSTEM if ai else Role.STAFF
        self.title = ""
        self.version_number = 1
        self.timestamp = datetime.now(timezone.utc)


def _kinds(entries):
    return [c.kind for c in contradictions.detect(entries)]


# ==========================================================================
# What must now fire
# ==========================================================================


def test_allergy_at_minute_two_conflicts_with_a_prescription_at_minute_nineteen():
    """Scenario 7's pairing, inside one transcript.

    Previously undetectable at any point in the consult's life: not during
    (nothing is incremental — see test_capture_timing.py) and not after
    either, because both statements landed in the same Entry and the detector
    never compared an entry with itself.
    """
    found = contradictions.detect(
        [
            _Entry(
                "consult-1",
                "Patient says she is allergic to penicillin. "
                "Chest is clear on auscultation. "
                "I will start you on amoxicillin 500mg three times a day.",
            )
        ]
    )
    assert [c.kind for c in found] == ["allergy_vs_administration"]
    assert str(found[0].severity) == "critical"
    assert found[0].same_entry is True
    # Both sides still cite a real, openable source.
    assert found[0].left_pointer and found[0].right_pointer
    assert "penicillin" in found[0].left_quote.lower()


def test_a_denial_and_an_assertion_in_one_session_are_reconciled_not_averaged():
    found = contradictions.detect(
        [
            _Entry(
                "session-1",
                "Patient reports no known allergies. "
                "Later in the consult she mentioned she is allergic to penicillin.",
            )
        ]
    )
    assert "assertion_vs_denial" in [c.kind for c in found]


def test_a_spoken_self_correction_is_reported_as_a_correction():
    """Two doses, one speaker, a retraction between them.

    A typed note gets edited, so the correction never reaches the text. A
    spoken one carries the retraction inline and the transcript keeps both
    figures — which is why this class exists only for same-entry pairs.
    """
    found = contradictions.detect(
        [
            _Entry(
                "consult-2",
                "I will start amoxicillin 500mg three times a day. "
                "Sorry, correction, make that amoxicillin 250mg three times a day.",
            )
        ]
    )
    assert [c.kind for c in found] == ["self_correction"]
    item = found[0]
    assert item.same_entry is True
    # MEDIUM, not HIGH: this one is already resolved. It is a thing to verify
    # against the audio, not a reconciliation task with no answer.
    assert str(item.severity) == "medium"
    # Both figures are shown. A mis-heard correction reads exactly like a real
    # one, so the system reports rather than silently applying the later value.
    assert "500" in item.detail and "250" in item.detail


@pytest.mark.parametrize(
    "cue",
    ["Sorry, make that", "Correction:", "I meant", "Scratch that,", "That should be"],
)
def test_each_correction_cue_is_recognised(cue):
    found = contradictions.detect(
        [
            _Entry(
                "c",
                f"Start metformin 1000mg BD. {cue} metformin 500mg BD.",
            )
        ]
    )
    assert "self_correction" in [c.kind for c in found], f"cue not recognised: {cue}"


def test_no_wait_is_a_known_miss_because_negation_scope_eats_it():
    """A limitation pinned rather than hidden.

    "No wait, metformin 500" is an ordinary spoken correction and this build
    does not catch it: `_negated` sees the leading "no" ahead of the drug and
    drops the claim before any correction cue is consulted. Fixing it means
    changing negation scope, which two modules share, for one phrase.

    This test exists to fail on the day negation scope improves — at which
    point "no wait" belongs back in `_CORRECTION_CUES` and this test should be
    rewritten, not deleted.
    """
    found = contradictions.detect(
        [_Entry("c", "Start metformin 1000mg BD. No wait, metformin 500mg BD.")]
    )
    assert [c.kind for c in found] == [], (
        "negation scope now handles 'no wait' — put the cue back and update this test"
    )


# ==========================================================================
# What must stay quiet — the alert-fatigue half
# ==========================================================================


def test_deliberation_about_two_doses_is_not_a_contradiction():
    """"We could use 500 or 1000" is thinking out loud, not disagreement.

    This is why the dose class is gated behind an explicit correction cue
    rather than firing on any two differing doses in one entry.
    """
    assert (
        _kinds(
            [
                _Entry(
                    "c",
                    "We could use metformin 500mg or metformin 1000mg "
                    "depending on how she tolerates it.",
                )
            ]
        )
        == []
    )


def test_ordinary_stop_and_start_sequencing_in_one_consult_stays_quiet():
    """A consult that switches a drug says stop and start constantly.

    Status is deliberately excluded from the intra-entry pass for exactly this
    reason — narrative sequencing and disagreement look identical in one entry.
    """
    assert (
        _kinds(
            [
                _Entry(
                    "c",
                    "Stop the amlodipine from tomorrow. "
                    "Continue the amlodipine until then.",
                )
            ]
        )
        == []
    )


def test_a_dose_stated_once_produces_nothing():
    assert _kinds([_Entry("c", "Started on metformin 500mg BD.")]) == []


# ==========================================================================
# The cross-entry behaviour must be untouched
# ==========================================================================


def test_cross_entry_allergy_conflict_is_unchanged():
    found = contradictions.detect(
        [
            _Entry("n1", "Patient states she is allergic to penicillin.", ai=False),
            _Entry("n2", "Started on amoxicillin 500mg TDS.", ai=False),
        ]
    )
    assert [c.kind for c in found] == ["allergy_vs_administration"]
    assert found[0].same_entry is False
    assert found[0].human_human is True


def test_two_authors_disagreeing_on_a_dose_is_still_a_disagreement_not_a_correction():
    """The correction class must not swallow the two-author case.

    Between two entries nobody retracted anything — both figures are live and
    a person has to decide which is current. That is a different card.
    """
    found = contradictions.detect(
        [
            _Entry("n1", "Metformin 1g BD.", ai=False),
            _Entry("n2", "Metformin 500mg BD.", ai=False),
        ]
    )
    assert [c.kind for c in found] == ["dose_disagreement"]
    assert found[0].same_entry is False


def test_a_correction_cue_across_two_entries_is_not_a_self_correction():
    """Cues only count inside one entry. Two entries are two authors, whatever
    words the second one happens to use."""
    found = contradictions.detect(
        [
            _Entry("n1", "Start metformin 1000mg BD.", ai=False),
            _Entry("n2", "Correction: metformin 500mg BD.", ai=False),
        ]
    )
    assert [c.kind for c in found] == ["dose_disagreement"]
