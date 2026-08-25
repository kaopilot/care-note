"""Redaction chokepoint tests (Phase 0, step 5).

All sample text below is obviously fake and written for this test file.
"""

from __future__ import annotations

import pytest

from app.ai.redaction import find_residual_phi, redact_phi, redact_phi_detailed


# --------------------------------------------------------------------------
# IC / ID numbers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sample",
    [
        "Patient NRIC S1234567D was verified at reception.",
        "IC number T9876543A on file.",
        "MyKad 880101-14-5678 recorded.",
    ],
)
def test_id_numbers_are_removed(sample: str) -> None:
    out = redact_phi(sample)
    assert "S1234567D" not in out
    assert "T9876543A" not in out
    assert "880101-14-5678" not in out
    assert "[ID_" in out


def test_mrn_label_is_redacted() -> None:
    out = redact_phi("Filed under MRN: A-40192 for follow-up.")
    assert "A-40192" not in out
    assert "[ID_" in out


# --------------------------------------------------------------------------
# Phone numbers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sample,secret",
    [
        ("Contact number is +65 9123 4567.", "9123 4567"),
        ("Call her at 8123 4567 after 6pm.", "8123 4567"),
        ("Mobile: +60 12-345 6789", "12-345 6789"),
        ("Tel 6789 1234 for the clinic line.", "6789 1234"),
    ],
)
def test_phone_numbers_are_removed(sample: str, secret: str) -> None:
    out = redact_phi(sample)
    assert secret not in out
    assert "[PHONE_" in out


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------


def test_honorific_names_are_removed() -> None:
    out = redact_phi("Dr Lim Wei Sheng reviewed the chart with Mdm Amira Rahman.")
    assert "Lim Wei Sheng" not in out
    assert "Amira Rahman" not in out
    assert out.count("[NAME_") == 2


def test_labelled_names_are_removed() -> None:
    out = redact_phi("Patient: Daniel Choo\nSeen by: Grace Tan")
    assert "Daniel Choo" not in out
    assert "Grace Tan" not in out


def test_gazetteer_catches_bare_first_names() -> None:
    """A bare first name in prose has no pattern to anchor on; the gazetteer of
    known synthetic names in scope is what catches it."""
    text = "Amira mentioned the cough is worse at night."
    assert "Amira" in redact_phi(text)  # no gazetteer -> not caught, honestly
    result = redact_phi_detailed(text, gazetteer={"Amira Rahman", "Amira"})
    assert "Amira" not in result.text
    assert "[NAME_1]" in result.text


def test_patronymic_names_are_removed() -> None:
    out = redact_phi("Encik Rahman bin Ismail attended with his daughter.")
    assert "Rahman bin Ismail" not in out


# --------------------------------------------------------------------------
# Behaviour of the pass as a whole
# --------------------------------------------------------------------------


def test_same_value_maps_to_same_placeholder() -> None:
    """Consistency matters: the LLM must still be able to tell that two
    mentions are the same person."""
    result = redact_phi_detailed(
        "Dr Lim ordered labs. Dr Lim will review them on Friday.",
    )
    assert result.text.count("[NAME_1]") == 2
    assert "[NAME_2]" not in result.text


def test_redaction_is_idempotent() -> None:
    """llm_client redacts unconditionally, so a second pass must be a no-op."""
    once = redact_phi("Dr Lim, NRIC S1234567D, tel +65 9123 4567.")
    twice = redact_phi(once)
    assert once == twice


def test_clinical_content_survives() -> None:
    """Over-redaction destroys the summary. Drug names and findings must stay."""
    text = "Metformin 500mg BD started. HbA1c 8.2%. Reports morning dizziness."
    out = redact_phi(text)
    assert "Metformin 500mg BD" in out
    assert "HbA1c 8.2%" in out
    assert "dizziness" in out


def test_empty_and_none_safe() -> None:
    """Contract is str -> str. None collapses to "" rather than propagating:
    a redaction function must never return something un-redactable."""
    assert redact_phi("") == ""
    assert redact_phi(None) == ""  # type: ignore[arg-type]


def test_names_do_not_bleed_across_lines() -> None:
    """Regression: \\s in the name patterns let a match run past a newline and
    swallow the next line's label, leaving the second person unredacted."""
    out = redact_phi("Patient: Daniel Choo\nSeen by: Grace Tan")
    assert "Daniel Choo" not in out
    assert "Grace Tan" not in out
    assert out.count("[NAME_") == 2


def test_result_reports_counts() -> None:
    result = redact_phi_detailed("Dr Lim (NRIC S1234567D) can be reached at +65 9123 4567.")
    assert result.replacements >= 3
    assert result.by_category.get("nric") == 1
    assert result.by_category.get("phone") == 1
    assert not result.clean


def test_placeholder_map_does_not_retain_originals() -> None:
    """A reversible mapping would be a second copy of the PHI. It must not exist."""
    result = redact_phi_detailed("Dr Lim, NRIC S1234567D.")
    assert "S1234567D" not in str(result.placeholders)
    assert set(result.placeholders.values()) <= {"name", "nric", "phone", "email", "dob", "mrn"}


# --------------------------------------------------------------------------
# Fail-closed tripwire
# --------------------------------------------------------------------------


def test_find_residual_phi_flags_unredacted_text() -> None:
    assert "nric" in find_residual_phi("S1234567D")
    assert "email" in find_residual_phi("a.person@example.com")


def test_find_residual_phi_clean_after_redaction() -> None:
    text = "Dr Lim, NRIC S1234567D, email a.person@example.com, tel +65 9123 4567."
    assert find_residual_phi(redact_phi(text)) == []
