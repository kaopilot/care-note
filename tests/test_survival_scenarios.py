"""The sixteen clinic scenarios, as one runnable index.

The round-two deliverable asks for "automated tests based on scenarios 1-16".
Those tests exist, but they are organised by subsystem — which is right for
maintaining them and wrong for answering "show me scenario 9". This file is the
index: one test per scenario, each either asserting the behaviour directly or
naming the file that does and asserting the honest verdict.

**It does not restate coverage it does not have.** Where the build does not
survive, the test says so and pins the boundary, because a scenario index full
of passing tests that quietly skip the hard ones would be the same overclaiming
the brief warns about. `VERDICTS` below is the same table as
`docs/SCENARIO_COVERAGE.md`, and `test_the_index_matches_the_published_table`
fails if the two drift.

Run just this file for a scenario-by-scenario walkthrough:

    pytest tests/test_survival_scenarios.py -v
"""

from __future__ import annotations

import pytest

from app.core.enums import Role
from app.security.rbac import AccessScope

# Scenario -> (verdict, where the real assertions live).
VERDICTS: dict[int, tuple[str, str]] = {
    1: ("SURVIVES", "test_enrolment.py"),
    2: ("PARTIAL", "test_rbac_scope.py, test_phase1_cross_clinic.py, and below"),
    3: ("PARTIAL", "test_failure_modes.py, test_url_surface.py"),
    4: ("SURVIVES", "test_llm_chokepoint.py"),
    5: ("PARTIAL", "test_clinic_config.py — vocabulary still global"),
    6: ("PARTIAL", "test_language_risk_floor.py, test_multilingual_features.py"),
    7: ("DOES NOT", "test_capture_timing.py — batch boundary pinned deliberately"),
    8: ("PARTIAL", "test_failure_modes.py — 8s timeout, no server-side abort"),
    9: ("SURVIVES", "test_failure_modes.py, EntryCard.degraded.test.jsx"),
    10: ("SURVIVES", "test_concurrent_edits.py"),
    11: ("PARTIAL", "test_delivery_state.py — reach modelled, no sender"),
    12: ("SURVIVES", "test_regeneration_and_dosage.py, test_delivery_state.py"),
    13: ("SURVIVES", "test_contradiction_denial.py, test_contradiction_grouping.py"),
    14: ("SURVIVES", "test_evaluation_and_abstention.py, test_language_risk_floor.py"),
    15: ("SURVIVES", "test_self_learning_importance.py, test_protected_surface.py"),
    16: ("SURVIVES", "test_highlight_provenance.py"),
}


def test_the_index_covers_every_scenario():
    assert sorted(VERDICTS) == list(range(1, 17))


def test_the_index_matches_the_published_table():
    """The doc and the tests must not drift.

    `SCENARIO_COVERAGE.md` is what a reviewer reads; this dict is what runs. If
    they disagree, the document is decoration.
    """
    import pathlib
    import re

    doc = pathlib.Path(__file__).resolve().parents[1] / "docs" / "SCENARIO_COVERAGE.md"
    text = doc.read_text()

    published: dict[int, str] = {}
    for line in text.splitlines():
        match = re.match(r"\|\s*(\d+)\s*\|.*?\|\s*\*\*(SURVIVES|PARTIAL|DOES NOT)\*\*", line)
        if match:
            published[int(match.group(1))] = match.group(2)

    assert published, "could not parse the verdict table out of SCENARIO_COVERAGE.md"
    for number, (verdict, _) in VERDICTS.items():
        assert published.get(number) == verdict, (
            f"scenario {number}: this file says {verdict}, the document says "
            f"{published.get(number)}"
        )


# --- Scenario 2, in full ---------------------------------------------------
#
# "Name the single place clinic isolation is actually enforced. Now assume that
# line has a bug. How many patients become visible to the wrong clinic, and what
# else would have caught it?"
#
# The answer is `AccessScope.query`, and it is genuinely one place. The two
# tests below answer the second half of the question honestly rather than
# asserting the happy path a third time.


def test_clinic_isolation_holds_normally(client, seeded, token_for):
    """The control works."""
    clinician_a = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    assert client.get("/patients/patient-b1", headers=clinician_a).status_code == 404


def test_breaking_the_single_line_exposes_every_clinic(
    client, seeded, token_for, monkeypatch
):
    """The blast radius, measured rather than estimated.

    This deliberately reintroduces the bug scenario 2 posits — the clinic
    predicate dropped from `AccessScope.query` — and records what happens.

    **Nothing else catches it.** There is no second, independent layer: no
    row-level security in SQLite, no per-tenant connection, no assertion at the
    serialisation boundary. Every route reaches data through this one method, so
    one wrong line exposes every patient in every clinic, not a subset.

    That is why scenario 2 is PARTIAL and not SURVIVES. The enforcement is
    strong — fused into a type, impossible to forget, proven server-side — and
    it is singular, which is a different property. This test is the evidence for
    saying so, and it will start failing the day a genuine second layer is added,
    which is the right time to revisit the verdict.

    See DECISIONS.md D-085.
    """
    clinician_a = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")

    def unscoped(self, model):
        return self.db.query(model)  # the clinic predicate, dropped

    monkeypatch.setattr(AccessScope, "query", unscoped)

    leaked = client.get("/patients/patient-b1", headers=clinician_a)
    assert leaked.status_code == 200, (
        "if this now 404s, a second enforcement layer exists — update the "
        "scenario 2 verdict and this docstring"
    )

    listing = client.get("/patients", headers=clinician_a)
    ids = {row["id"] for row in listing.json()}
    assert "patient-b1" in ids, "the whole other clinic is visible, not one record"


# --- Scenario 7, the boundary we do not cross ------------------------------


def test_scenario_7_is_honestly_marked_as_not_surviving():
    """A drug allergy at minute two is not known until the consult ends.

    The scribe pipeline is post-hoc: it consumes a complete transcript. There is
    no incremental extraction, so nothing reaches the Glance View mid-consult.
    `test_capture_timing.py` pins that boundary with real assertions; this exists
    so a reader of the scenario index cannot mistake silence for coverage.
    """
    verdict, _ = VERDICTS[7]
    assert verdict == "DOES NOT"
