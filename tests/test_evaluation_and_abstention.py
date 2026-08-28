"""Evaluation and abstention: what each number means, and how we'd know it was wrong.

The 48-hour hint asks three questions of the risk badge, the confidence label
and the importance score: what is it, how would we know if it were wrong, and
what happens when it is. A number nobody can falsify is decoration, so this file
is the falsification. Each test states the claim the UI makes and then tries to
break it.

Grouped by the claim under test rather than by module, because the point is the
claim.
"""

from __future__ import annotations

import pytest

from app.core.enums import AI_SCRIBED_TYPES, EntryType, InteractionType, RiskLevel, Role
from app.security import policy
from app.services import contradictions as contradiction_service
from app.services import highlights as highlight_service
from app.services import scribe


# ==========================================================================
# The risk badge — "a rule can raise this, a model can never lower it"
# ==========================================================================


class _Entry:
    """Minimal stand-in; the detector reads only id, content and type."""

    def __init__(self, id_: str, content: str, type_=EntryType.STAFF_NOTE):
        self.id = id_
        self.content = content
        self.type = str(type_)


def test_deterministic_floor_overrides_a_model_that_understates_risk():
    """The failure this exists to stop: a model calling a red flag routine.

    A transcript containing "chest pain" has a deterministic floor of `high`.
    A model proposing `low` must not be able to move the badge down.
    """
    transcript = "patient: I have been getting chest pain when I walk uphill."
    floor = RiskLevel(scribe._infer_risk(transcript))
    assert floor is RiskLevel.HIGH

    resolved = scribe._max_risk(RiskLevel.LOW, floor)
    assert resolved is RiskLevel.HIGH, "a model was able to lower the risk badge"


def test_a_model_may_raise_risk_above_the_floor():
    """Asymmetric on purpose — the rules are a floor, not a ceiling.

    Keyword tables miss things. A model noticing something they do not is the
    case the floor must not suppress.
    """
    transcript = "patient: I have been a bit more tired than usual lately."
    floor = RiskLevel(scribe._infer_risk(transcript))
    assert floor is RiskLevel.LOW

    assert scribe._max_risk(RiskLevel.CRITICAL, floor) is RiskLevel.CRITICAL


@pytest.mark.parametrize(
    "term,expected",
    [
        ("chest pain", RiskLevel.HIGH),
        ("anaphylaxis", RiskLevel.HIGH),
        ("suicidal", RiskLevel.HIGH),
        ("sepsis", RiskLevel.HIGH),
    ],
)
def test_the_floor_is_deterministic_across_runs(term, expected):
    """Ordinals that drift between runs are the hint's stated concern.

    Same input, same answer, every time — that is the whole argument for a rule
    rather than a judgement here.
    """
    transcript = f"clinician: documenting {term} today."
    first = RiskLevel(scribe._infer_risk(transcript))
    assert first is expected
    for _ in range(5):
        assert RiskLevel(scribe._infer_risk(transcript)) is first


def test_the_scribe_records_when_the_floor_was_applied(client_p1, token_for):
    """"Why does this say high?" must be answerable from the row.

    Not from a comment in the source, and not from re-running the model.
    """
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    response = client_p1.post(
        "/patients/patient-a1/scribe",
        json={"interaction_type": str(InteractionType.DOCTOR_PATIENT_CONSULT)},
        headers=headers,
    )
    assert response.status_code == 201
    body = response.json()
    # The field exists and is a real boolean either way — its absence, not its
    # value, would be the failure.
    assert "risk_floor_applied" in body
    assert isinstance(body["risk_floor_applied"], bool)


# ==========================================================================
# The confidence label — "medium means a number, and the number means something"
# ==========================================================================


def test_confidence_bands_have_numeric_boundaries():
    """"Medium" is not a vibe. It is 0.60 to 0.75."""
    assert scribe.confidence_band(0.90) == "high"
    assert scribe.confidence_band(scribe.CONFIDENCE_HIGH_BAND) == "high"
    assert scribe.confidence_band(0.70) == "medium"
    assert scribe.confidence_band(scribe.CONFIDENCE_LOW_BAND) == "medium"
    assert scribe.confidence_band(0.59) == "low"
    assert scribe.confidence_band(None) == "unknown"


def test_the_ui_flag_and_the_band_use_the_same_number():
    """A card that says "medium" while the low-confidence flag fires is worse
    than either alone, because it makes the reader distrust both."""
    from app.services import glance

    assert glance.LOW_CONFIDENCE_THRESHOLD == scribe.CONFIDENCE_LOW_BAND


def test_confidence_falls_as_the_source_gets_more_hedged():
    """The claim the chip makes: this is measured from the transcript.

    Falsifiable, and here falsified against a plain transcript and a hedged one.
    """
    plain = (
        "clinician: Blood pressure is 148 over 92 today.\n"
        "clinician: HbA1c came back at 8.4 percent.\n"
        "clinician: Starting metformin 500mg twice daily.\n"
    )
    hedged = (
        "patient: I think maybe the dizziness started a while back, not sure.\n"
        "patient: It might be the new tablets, possibly, I guess.\n"
        "patient: I suppose it could have been earlier, maybe.\n"
    )
    assert scribe.derived_confidence(plain) > scribe.derived_confidence(hedged)


def test_confidence_never_claims_certainty():
    """A summariser reading a transcript it did not hear, through a recogniser
    that may have erred, has no business reporting 1.0."""
    assert scribe.derived_confidence("clinician: BP 120 over 80.") <= scribe.CONFIDENCE_CEILING
    assert scribe.CONFIDENCE_CEILING < 1.0
    assert scribe.derived_confidence("patient: maybe, I think, not sure, possibly.") >= (
        scribe.CONFIDENCE_FLOOR
    )


def test_derived_confidence_is_deterministic():
    text = "patient: I think it might be getting worse, maybe."
    values = {scribe.derived_confidence(text) for _ in range(5)}
    assert len(values) == 1


def test_model_self_report_is_recorded_but_not_displayed(client_p1, token_for):
    """Self-reported confidence is kept for calibration and never shown.

    On the offline path there is no self-report at all, so the displayed figure
    cannot possibly be one.
    """
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    created = client_p1.post(
        "/patients/patient-a1/scribe",
        json={"interaction_type": str(InteractionType.AI_PATIENT_SESSION)},
        headers=headers,
    ).json()

    entries = client_p1.get("/patients/patient-a1/entries", headers=headers).json()
    entry = next(e for e in entries if e["id"] == created["id"])
    assert entry["ai_confidence"] is not None
    assert entry["ai_confidence_band"] in {"high", "medium", "low"}
    # What the model claimed about itself is not on the wire at all.
    assert "model_self_reported_confidence" not in entry


# ==========================================================================
# Patient-facing content — a higher severity class, enforced structurally
# ==========================================================================


def test_no_generated_entry_type_is_patient_facing():
    """The guard that makes the claim true rather than aspirational."""
    for entry_type in scribe.SUMMARY_TYPE.values():
        assert not policy.is_patient_facing(entry_type)
    policy.assert_never_patient_facing(scribe.SUMMARY_TYPE.values())


def test_the_guard_actually_fires():
    """A guard nobody has seen fail is not known to work."""
    with pytest.raises(policy.PatientFacingAuthorshipError):
        policy.assert_never_patient_facing([EntryType.PATIENT_INSTRUCTION])


def test_only_clinicians_may_author_patient_facing_content():
    """Not staff, not admin, and above all not system."""
    for entry_type in policy.PATIENT_FACING_TYPES:
        assert policy.can_write_type(Role.CLINICIAN, entry_type)
        assert not policy.can_write_type(Role.STAFF, entry_type)
        assert not policy.can_write_type(Role.ADMIN, entry_type)
        assert not policy.can_write_type(Role.SYSTEM, entry_type)
        assert not policy.can_write_type(Role.PATIENT, entry_type)


def test_a_patient_cannot_see_ai_scribed_content_at_all():
    """Belt and braces: even a mislabelled AI note is unreadable by a patient."""
    for entry_type in AI_SCRIBED_TYPES:
        assert not policy.can_view_type(Role.PATIENT, entry_type)


def test_staff_cannot_write_a_patient_instruction_over_the_api(client_p1, token_for):
    """The structural rule, proven server-side rather than in the matrix."""
    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    response = client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": "patient_instruction", "content": "Take two tablets daily."},
        headers=headers,
    )
    assert response.status_code == 403


# ==========================================================================
# Contradictions — human-human, deterministic, and never auto-resolved
# ==========================================================================


def test_allergy_against_administration_is_caught_across_drug_class():
    """The one that kills people: penicillin allergy, amoxicillin prescribed."""
    found = contradiction_service.detect(
        [
            _Entry("nurse-1", "Patient reports allergic to penicillin, rash."),
            _Entry("clin-1", "Started on amoxicillin 500mg TDS for chest infection."),
        ]
    )
    assert len(found) == 1
    item = found[0]
    assert item.kind == "allergy_vs_administration"
    assert item.severity is RiskLevel.CRITICAL
    assert item.human_human is True
    assert {item.left_entry_id, item.right_entry_id} == {"nurse-1", "clin-1"}


def test_a_contradiction_always_cites_both_sides():
    """A flag pointing at one entry is an assertion. Pointing at two is evidence."""
    found = contradiction_service.detect(
        [
            _Entry("a", "Allergic to penicillin."),
            _Entry("b", "Amoxicillin 500mg started."),
        ]
    )
    item = found[0]
    for pointer, entry_id in (
        (item.left_pointer, item.left_entry_id),
        (item.right_pointer, item.right_entry_id),
    ):
        assert entry_id in pointer
    assert item.left_quote and item.right_quote


def test_dose_disagreement_is_caught_without_an_action_verb():
    """Half of real notes record a drug as "Metformin 1g BD" and nothing else."""
    found = contradiction_service.detect(
        [
            _Entry("a", "Metformin 1g BD continued."),
            _Entry("b", "Metformin 500mg BD as per plan."),
        ]
    )
    assert [f.kind for f in found] == ["dose_disagreement"]
    assert found[0].severity is RiskLevel.HIGH


def test_the_same_dose_in_different_units_is_not_a_contradiction():
    """False positives on doses are how a flag gets trained out of usefulness."""
    assert (
        contradiction_service.detect(
            [_Entry("a", "Metformin 1g BD."), _Entry("b", "Metformin 1000mg BD.")]
        )
        == []
    )


def test_agreeing_notes_produce_nothing():
    assert (
        contradiction_service.detect(
            [
                _Entry("a", "Metformin 500mg BD started."),
                _Entry("b", "Metformin 500mg BD continued."),
            ]
        )
        == []
    )


@pytest.mark.parametrize(
    "allergy_text",
    [
        "Patient denies allergy to aspirin.",
        "No allergy to aspirin documented.",
        "Nil allergy to aspirin.",
    ],
)
def test_a_negated_allergy_is_not_an_allergy(allergy_text):
    """Reading "denies allergy to aspirin" as an allergy would fire a critical
    flag on a patient who has none — the fastest possible way to teach a
    clinician that critical flags are noise."""
    assert (
        contradiction_service.detect(
            [_Entry("a", allergy_text), _Entry("b", "Aspirin 75mg given.")]
        )
        == []
    )


def test_status_disagreement_is_caught():
    found = contradiction_service.detect(
        [
            _Entry("a", "Warfarin stopped ahead of procedure."),
            _Entry("b", "Warfarin 5mg continue as before."),
        ]
    )
    assert [f.kind for f in found] == ["status_disagreement"]


def test_detection_is_deterministic():
    """No model, so the same chart must give the same answer every time."""
    entries = [
        _Entry("a", "Allergic to penicillin."),
        _Entry("b", "Amoxicillin 500mg started."),
        _Entry("c", "Metformin 1g BD."),
        _Entry("d", "Metformin 500mg BD."),
    ]
    runs = [
        [(f.kind, f.subject, f.left_entry_id, f.right_entry_id) for f in contradiction_service.detect(entries)]
        for _ in range(5)
    ]
    assert all(run == runs[0] for run in runs)


def test_allergy_conflicts_sort_above_everything_else():
    """When the card truncates, it must drop status before it drops allergies."""
    found = contradiction_service.detect(
        [
            _Entry("a", "Warfarin stopped ahead of procedure."),
            _Entry("b", "Warfarin 5mg continue as before."),
            _Entry("c", "Allergic to penicillin."),
            _Entry("d", "Amoxicillin 500mg started."),
        ]
    )
    assert found[0].kind == "allergy_vs_administration"


def test_the_system_never_resolves_a_human_human_contradiction():
    """There is no precedence rule here and inventing one would be a clinical
    decision this system has no standing to make. Both sides survive."""
    found = contradiction_service.detect(
        [
            _Entry("older", "Allergic to penicillin."),
            _Entry("newer", "Amoxicillin 500mg started."),
        ]
    )
    item = found[0]
    assert item.left_entry_id and item.right_entry_id
    # No field on the finding expresses a winner — check the shape, not a value.
    assert not hasattr(item, "resolution")
    assert not hasattr(item, "winner")


def test_contradictions_reach_the_glance_view(client_p1, token_for):
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": "clinician_section", "content": "Allergic to penicillin, rash."},
        headers=headers,
    )
    client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": "clinician_section", "content": "Amoxicillin 500mg TDS started."},
        headers=headers,
    )
    card = client_p1.get("/patients/patient-a1/glance", headers=headers).json()
    found = card["contradictions"]
    assert found, "an allergy conflict did not reach the card"
    assert found[0]["severity"] == "critical"
    assert found[0]["left"]["quote"] and found[0]["right"]["quote"]


# ==========================================================================
# The importance score — exposure bias and the fatigue floor
# ==========================================================================


def test_an_unexposed_tag_can_displace_a_marginally_better_known_one():
    """Exposure bias: a candidate below the cut is never shown, so never gains
    weight, so is never shown. One slot is reserved to break that loop."""
    candidates = [
        (0.90, {"tags": ["med:warfarin"], "span_start": 0, "span_end": 5}),
        (0.80, {"tags": ["med:warfarin"], "span_start": 5, "span_end": 10}),
        (0.70, {"tags": ["med:warfarin"], "span_start": 10, "span_end": 15}),
        (0.60, {"tags": ["entity:allergy"], "span_start": 15, "span_end": 20}),
    ]
    kept = highlight_service._keep_with_exploration(candidates, existing=[])
    tags = {tag for _, spec in kept for tag in spec["tags"]}
    assert "entity:allergy" in tags, "no slot was given to an unexposed tag"
    assert len(kept) == highlight_service.MAX_SUGGESTIONS_PER_ENTRY
    # The strongest candidate is never displaced.
    assert kept[0][0] == 0.90


def test_exploration_is_deterministic_not_random():
    """An epsilon-greedy coin flip would make the card differ between loads.
    For a clinical surface that is worse than the bias it fixes."""
    candidates = [
        (0.90, {"tags": ["med:warfarin"], "span_start": 0, "span_end": 5}),
        (0.80, {"tags": ["med:warfarin"], "span_start": 5, "span_end": 10}),
        (0.70, {"tags": ["med:warfarin"], "span_start": 10, "span_end": 15}),
        (0.60, {"tags": ["entity:allergy"], "span_start": 15, "span_end": 20}),
    ]
    runs = [
        [(s, tuple(spec["tags"])) for s, spec in highlight_service._keep_with_exploration(candidates, [])]
        for _ in range(5)
    ]
    assert all(run == runs[0] for run in runs)


def test_exploration_cannot_surface_a_meaningless_span():
    """The slot re-ranks candidates the rules already found worth showing. A
    span with no tags is not a candidate at all."""
    candidates = [
        (0.90, {"tags": ["med:warfarin"], "span_start": 0, "span_end": 5}),
        (0.80, {"tags": ["med:warfarin"], "span_start": 5, "span_end": 10}),
        (0.70, {"tags": ["med:warfarin"], "span_start": 10, "span_end": 15}),
        (0.60, {"tags": [], "span_start": 15, "span_end": 20}),
    ]
    kept = highlight_service._keep_with_exploration(candidates, existing=[])
    assert all(spec["tags"] for _, spec in kept)


def test_safety_vocabulary_cannot_be_dismissed_into_silence():
    """Care-team fatigue: under load people dismiss things. Dismissing warfarin
    three times should quiet warfarin. Dismissing anaphylaxis three times must
    never quiet anaphylaxis."""
    from app.services.learning import NEVER_DAMPENED

    for tag in ("entity:allergy", "symptom:anaphylaxis", "symptom:sepsis", "risk:critical"):
        assert tag in NEVER_DAMPENED
