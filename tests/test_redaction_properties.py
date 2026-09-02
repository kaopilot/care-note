"""Redaction, tested by generated input rather than chosen input.

Why this file exists
--------------------
Six defects were found across two audits, and all six shared a cause: **each
test used the shape of the case its author had in mind.** Cross-entry
contradiction tests used two entries, so a one-entry transcript was invisible.
Abstention tests used romanised Latin script, so Chinese never fired. The
exposure-bias evaluator modelled the card its author assumed rather than the
card the product renders.

More tests of the same kind would not have caught any of them, because the
blind spot is in the *shape selection*, not the assertion. Property-based tests
attack exactly that: the author writes the invariant, and the library chooses
shapes — including the ones the author would never have thought to write down.

Two properties, and they pull in opposite directions
----------------------------------------------------
The 48-hour hint said "redaction is accuracy, not just privacy". That is one
sentence describing two failure modes that trade against each other:

* **Under-redaction** is a privacy breach: an IC number reaches the model.
* **Over-redaction** is a clinical safety problem: `[PHONE_1]` where the note
  said `BP 120/80`, or a dose eaten by a phone pattern. The note still looks
  fine. It is just wrong, and nobody can tell by reading it.

A regex chokepoint can trivially win either one alone — redact everything, or
redact nothing. Testing only the first is how a build ends up destroying
clinical values while reporting a clean privacy posture. So both are asserted
here, over generated input, and neither is allowed to be satisfied at the
other's expense.

The oracle for under-redaction is the build's own `find_residual_phi`, which
`llm_client` already uses as a fail-closed tripwire. That makes a violation
here not merely theoretical: residual PHI blocks the LLM call, so the clinician
gets a failed summary. Under-redaction is a privacy bug *and* an outage.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from app.ai.redaction import find_residual_phi, redact_phi

# Generated input is slower than a fixed fixture, and a clinical test suite that
# takes minutes stops being run. Capped deliberately; raise it when hunting.
SETTINGS = settings(
    max_examples=250,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ==========================================================================
# Generators — PHI in the forms this region actually writes it
# ==========================================================================

nric = st.builds(
    lambda prefix, digits, suffix: f"{prefix}{digits:07d}{suffix}",
    st.sampled_from("STFGM"),
    st.integers(min_value=0, max_value=9_999_999),
    st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
)

mykad = st.builds(
    lambda a, b, c: f"{a:06d}-{b:02d}-{c:04d}",
    st.integers(min_value=0, max_value=999_999),
    st.integers(min_value=0, max_value=99),
    st.integers(min_value=0, max_value=9_999),
)

sg_phone = st.builds(
    lambda lead, rest, sep: f"{lead}{rest[:3]}{sep}{rest[3:]}",
    st.sampled_from("89"),
    st.text(alphabet="0123456789", min_size=7, max_size=7),
    st.sampled_from(["", " ", "-"]),
)

intl_phone = st.builds(
    lambda cc, sep, body: f"+{cc}{sep}{body}",
    st.sampled_from(["65", "60", "1", "44"]),
    st.sampled_from(["", " ", "-", "."]),
    st.text(alphabet="0123456789", min_size=8, max_size=10),
)

email = st.builds(
    lambda user, domain, tld: f"{user}@{domain}.{tld}",
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789._-", min_size=1, max_size=12),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=2, max_size=10),
    st.sampled_from(["com", "sg", "my", "org", "com.sg"]),
)

phi_value = st.one_of(nric, mykad, sg_phone, intl_phone, email)

# Prose the PHI gets embedded in. Deliberately includes newlines and
# punctuation: a name match running off the end of a line was a real bug once
# (see the `_SP` comment in redaction.py), and generated input is how that
# class gets found rather than remembered.
PROSE_WORDS = [
    "patient", "reviewed", "in", "clinic", "today", "follow", "up", "in",
    "two", "weeks", "no", "acute", "concerns", "plan", "discussed", "and",
    "agreed", "wound", "clean", "review", "arranged", "contact", "details",
    "updated", "seen", "by", "the", "duty", "team",
]

prose = st.lists(st.sampled_from(PROSE_WORDS), min_size=0, max_size=14).map(" ".join)
separator = st.sampled_from([" ", "\n", ". ", ", ", " - ", ":\n", "\t", " (", ") "])


@st.composite
def text_containing_phi(draw):
    """One PHI value dropped into arbitrary prose at an arbitrary boundary."""
    value = draw(phi_value)
    return f"{draw(prose)}{draw(separator)}{value}{draw(separator)}{draw(prose)}".strip()


# ==========================================================================
# Property 1 — under-redaction: unambiguous PHI never survives
# ==========================================================================


@SETTINGS
@given(text=text_containing_phi())
def test_no_unambiguous_phi_survives_the_chokepoint(text):
    """The property the whole privacy posture rests on.

    `find_residual_phi` is the build's own definition of unambiguous PHI and is
    already wired into `llm_client` as a fail-closed tripwire, so anything it
    catches here would in production block the model call rather than leak. A
    failure is therefore both a privacy finding and an availability one.
    """
    assert find_residual_phi(redact_phi(text)) == []


@SETTINGS
@given(value=phi_value)
def test_phi_alone_on_a_line_is_redacted(value):
    """No surrounding prose at all — the degenerate shape a hand-written test
    rarely bothers with, and the one a pasted clipboard produces."""
    assert find_residual_phi(redact_phi(value)) == []


@SETTINGS
@given(values=st.lists(phi_value, min_size=2, max_size=5))
def test_several_identifiers_in_one_note_are_all_redacted(values):
    """Multi-PHI text. A pattern that consumes greedily can swallow the
    separator between two identifiers and leave the second one exposed."""
    text = ", ".join(values)
    assert find_residual_phi(redact_phi(text)) == []


@SETTINGS
@given(text=text_containing_phi())
def test_redaction_is_idempotent(text):
    """Redacting twice must equal redacting once.

    Not academic: the pipeline redacts on the way to the model, and a retry or
    a regeneration can re-run over already-redacted text. If placeholders are
    themselves matchable, `[PHONE_1]` becomes `[PHONE_[PHONE_2]]` and the audit
    count stops meaning anything.
    """
    once = redact_phi(text)
    assert redact_phi(once) == once


# ==========================================================================
# Property 2 — over-redaction: clinical values are not collateral damage
# ==========================================================================
#
# "Redaction is accuracy, not just privacy" (48-hour hint). A phone pattern is
# `\d{3,4}[-.\s]?\d{3,4}`, and clinical prose is full of digit pairs. If a dose
# or a vital sign gets replaced by `[PHONE_1]`, the note reads as complete and
# is wrong — the worst available failure, because nothing looks broken.

VITALS_AND_DOSES = [
    "BP 120/80",
    "BP 138/88 today, down from 150/95",
    "metformin 500mg BD",
    "amoxicillin 250mg three times a day",
    "levothyroxine 100mcg daily",
    "insulin 20 units nocte",
    "HbA1c 7.2%",
    "weight 68.4 kg",
    "temp 37.8",
    "pulse 88 regular",
    "sats 97% on room air",
    "INR 3.4, above range",
    "eGFR 58",
    "review in 3 months",
    "2 tablets QDS for 5 days",
    "creatinine 102 umol/L",
    "warfarin 5mg on alternate days",
    "paracetamol 1g QDS",
]


@pytest.mark.parametrize("clinical", VITALS_AND_DOSES)
def test_clinical_values_survive_redaction_unchanged(clinical):
    """Every number a clinician would act on must come out the other side.

    This is the half of "redaction is accuracy" that a privacy-only test suite
    never checks, and the half whose failures are invisible on the page.
    """
    assert redact_phi(clinical) == clinical


@SETTINGS
@given(
    before=prose,
    clinical=st.sampled_from(VITALS_AND_DOSES),
    after=prose,
)
def test_clinical_values_survive_in_arbitrary_surrounding_prose(before, clinical, after):
    """Position matters to a regex. The same dose at the start of a note, mid
    sentence, and after a cue word are three different inputs."""
    text = f"{before} {clinical} {after}".strip()
    assert clinical in redact_phi(text)


@SETTINGS
@given(text=prose)
def test_prose_with_no_phi_is_returned_unchanged(text):
    """The null case, asserted rather than assumed.

    A redactor that mangles ordinary sentences is one clinicians will route
    around, and routing around the chokepoint is the failure that matters.
    """
    assume(text.strip())
    assert redact_phi(text) == text


# ==========================================================================
# Property 3 — the function is total
# ==========================================================================


@SETTINGS
@given(text=st.text(max_size=400))
def test_redaction_never_raises_on_arbitrary_text(text):
    """Any unicode at all. This runs on transcripts, and a transcript can
    contain anything a microphone and an ASR pass between them produce —
    including scripts, control characters and mojibake nobody planned for.

    An exception here is a failed consult summary, which the clinician meets as
    a blank card while standing next to the patient.
    """
    result = redact_phi(text)
    assert isinstance(result, str)


@pytest.mark.parametrize("falsy", [None, "", "   "])
def test_falsy_input_yields_a_string_never_none(falsy):
    """A redaction function must never hand a caller something un-redactable."""
    assert isinstance(redact_phi(falsy), str)


# ==========================================================================
# Property 4 — the separator fold (D-095)
# ==========================================================================
#
# Found by generated input, then characterised by hand. The property tests
# above passed while "hp 9123–4567" leaked, because `find_residual_phi` — the
# oracle — spells its separator class in ASCII exactly like the redactor does.
# A shared blind spot between a check and its test is invisible twice.

DASHES = [
    ("ascii hyphen", "-"),
    ("hyphen", "\u2010"),
    ("non-breaking hyphen", "\u2011"),
    ("figure dash", "\u2012"),
    ("en dash", "\u2013"),
    ("em dash", "\u2014"),
    ("minus sign", "\u2212"),
    ("fullwidth hyphen", "\uff0d"),
]


@pytest.mark.parametrize("name,dash", DASHES)
def test_a_phone_number_cannot_hide_behind_a_typographic_dash(name, dash):
    """iOS and macOS autocorrect a hyphen between digits into an en-dash, and
    so does pasting from Word. This build's premise is text arriving from
    phones and transcripts, so that is the ordinary path, not the exotic one."""
    out = redact_phi(f"hp 9123{dash}4567")
    assert "9123" not in out, f"{name} defeated the phone pattern"
    assert find_residual_phi(out) == []


@pytest.mark.parametrize("name,dash", DASHES)
def test_the_tripwire_does_not_share_the_redactors_blind_spot(name, dash):
    """`find_residual_phi` must catch what the redactor missed, which it cannot
    do while it spells its separators the same way. Asserted directly against
    un-redacted text: this is the fail-closed check in `llm_client`, and it has
    to fire on input the redactor never saw."""
    assert find_residual_phi(f"9123{dash}4567") != []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Review in 2\u20133 weeks.", "Review in 2-3 weeks."),
        ("BP range 120\u2013140 systolic.", "BP range 120-140 systolic."),
        ("Dose 5\u201310mg as needed.", "Dose 5-10mg as needed."),
    ],
)
def test_folding_changes_dash_shape_and_never_clinical_values(text, expected):
    """The fold's only permitted side effect.

    Digits, units and words must survive identically; a hyphen may change
    shape. This output is LLM input — the stored Entry and transcript keep the
    author's original characters — so the cost is a glyph in a prompt.
    """
    assert redact_phi(text) == expected


# ==========================================================================
# A leak found and deliberately NOT fixed — pinned so it stays visible
# ==========================================================================


@pytest.mark.parametrize(
    "text",
    [
        "S 1234567 A",
        "S1234567 A",
        "900101 01 5432",
    ],
)
def test_space_separated_identifiers_are_a_known_leak(text):
    """An identifier read aloud and transcribed with spaces is not redacted.

    Found by the same probing pass as D-095 and left alone, because the fix is
    riskier than the bug. Widening `NRIC_RE` to tolerate internal spaces makes
    it match `T 1234567 B`-shaped fragments of ordinary prose, and widening the
    MyKad pattern to `\\d{6}\\s\\d{2}\\s\\d{4}` puts every run of grouped digits
    — lab panels, vitals series — at risk of being replaced by `[ID_1]`. That
    trades a narrow privacy gap for a broad accuracy one, which is the wrong
    direction for a clinical record (the 48-hour hint's point exactly).

    It matters most on the voice path, where a patient reading an IC number
    aloud is precisely how spaces get introduced. The honest mitigation is a
    cue-anchored pass over ASR output specifically, not a wider global regex.

    This test asserts the CURRENT behaviour, not the desired one. It fails the
    day someone fixes it, which is when the trade-off above should be re-argued
    rather than silently won. See DECISIONS.md D-095.
    """
    assert text in redact_phi(text), (
        "space-separated identifiers are now redacted — good. Re-check the "
        "false-positive risk on grouped clinical digits, then update this test."
    )
