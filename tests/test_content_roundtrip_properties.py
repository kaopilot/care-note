"""What an author writes is what the record holds, for arbitrary input.

D-015 makes two promises that pull against each other, and a test suite can
easily verify one while the other quietly fails:

* **Nothing is executed.** Note and comment bodies are untrusted, multi-author
  content, stored as plain text and never rendered as HTML.
* **Nothing is altered.** Clinical prose legitimately contains `BP <120/80` and
  `dose <5mg`. Escaping on write double-escapes on render; tag-stripping can
  silently delete a dose limit. The author's characters survive to storage.

The tempting implementation satisfies the first by breaking the second — strip
tags on write, and no payload can ever execute. It also turns `dose <5mg` into
`dose ` and nobody notices, because the note still reads as a sentence. That is
the worst class of failure in this build: wrong, and invisible on the page.

So the properties here are stated as a **round trip**. Whatever goes in comes
back byte-identical, modulo the four normalisations `sanitize_for_storage`
documents (NFC, line endings, control characters, length cap). Anything else
changing is a defect, whichever direction it moves in.

Generated input rather than chosen input, for the reason the rest of this
suite now works that way: the payloads a test author thinks to write are the
payloads they already defended against.
"""

from __future__ import annotations

import unicodedata

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from app.core.enums import Role
from app.core.sanitization import (
    escape_html,
    find_injection_markers,
    prepare_content,
    sanitize_for_storage,
)

SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

# Characters clinical prose actually contains and a naive sanitiser eats.
CLINICAL_LITERALS = [
    "BP <120/80",
    "dose <5mg",
    "keep INR >2.0 and <3.0",
    "sats >94% on air",
    "weight loss >5% in 3 months",
    "titrate to <7% HbA1c",
    "eGFR <30 — refer renal",
    "R&D referral pending",
    "Q&A with the family done",
    "allergy: penicillin & cephalosporins",
    'patient said "it burns when I walk"',
    "dose 5mg/kg/day",
    "1/2 tablet BD",
]

# Payloads that must be stored literally and never executed. Storing them
# verbatim is the correct behaviour — React escapes at render, which is the
# boundary where escaping belongs.
INJECTION_LITERALS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "javascript:alert(1)",
    "<svg/onload=alert(1)>",
    "'; DROP TABLE entries; --",
    "{{constructor.constructor('alert(1)')()}}",
    "<iframe src='evil'></iframe>",
    "&lt;script&gt;",
]


# Payloads that are real elsewhere and inert here. Listed explicitly rather
# than left out of the fixtures, because "we do not flag this" should be a
# recorded decision with a reason attached, not an absence.
#
#   * `{{constructor...}}` is Angular/Vue template injection. This frontend is
#     React, whose text children do not interpolate braces, and there is no
#     server-side template engine touching author content — no Jinja, no
#     Markdown renderer, no `dangerouslySetInnerHTML` anywhere (asserted by
#     `test_frontend_never_renders_raw_html`). Flagging it would add audit-log
#     noise for a construct that cannot execute.
#   * `&lt;script&gt;` is already-escaped text. A clinician pasting an error
#     message writes this legitimately.
#   * SQL-shaped prose is not the injection vector here — every query goes
#     through SQLAlchemy's parameter binding.
#
# **This list is a claim about the stack, not about the payload.** If a
# Markdown renderer or a server-side template is ever introduced, the entries
# above stop being inert and this list must shrink. D-015 already requires
# `html: false` on any Markdown renderer for the same reason.
NOT_APPLICABLE_TO_THIS_STACK = {
    "{{constructor.constructor('alert(1)')()}}",
    "&lt;script&gt;",
    "'; DROP TABLE entries; --",
}


# ==========================================================================
# The round trip, as a pure property
# ==========================================================================


@SETTINGS
@given(text=st.text(max_size=500))
def test_sanitisation_is_idempotent(text):
    """Sanitising twice must equal sanitising once.

    Every edit re-runs `prepare_content` on content that has already been
    through it (2.6 — a payload must not be able to enter via a later version).
    If the function is not idempotent, an entry's text drifts a little with
    every edit and nobody can point at the change that did it.
    """
    once = sanitize_for_storage(text)
    assert sanitize_for_storage(once) == once


@SETTINGS
@given(text=st.text(max_size=500))
def test_sanitisation_only_ever_does_the_four_documented_things(text):
    """The docstring lists NFC, line endings, control characters, a length cap.

    This asserts the list is exhaustive by reconstructing it independently: if
    someone later adds tag-stripping or escaping to the same function, the two
    computations diverge and this fails. A docstring that has quietly stopped
    being true is worse than no docstring.
    """
    expected = unicodedata.normalize("NFC", text)
    expected = expected.replace("\r\n", "\n").replace("\r", "\n")
    # Mirrors `_CONTROL_CHARS` exactly: C0 minus \t \n, plus DEL. The first
    # version of this reconstruction forgot DEL (U+007F) and failed — which is
    # the reconstruction working. An independent recomputation that agrees by
    # accident proves nothing.
    expected = "".join(
        ch
        for ch in expected
        if not (ord(ch) < 32 and ch not in "\t\n") and ord(ch) != 0x7F
    )
    assume(len(expected) <= 20_000)
    assert sanitize_for_storage(text) == expected


@pytest.mark.parametrize("clinical", CLINICAL_LITERALS)
def test_clinical_prose_survives_storage_unchanged(clinical):
    """The half a security-only test never checks.

    `dose <5mg` becoming `dose ` is a silent clinical error. The note still
    reads as a sentence; the limit is simply gone.
    """
    stored, _ = prepare_content(clinical)
    assert stored == clinical


@pytest.mark.parametrize("payload", INJECTION_LITERALS)
def test_injection_payloads_are_stored_literally_not_stripped(payload):
    """Storing verbatim IS the defence, not a gap in it.

    Escaping belongs at the render boundary, where React already does it.
    Stripping here would mean the audit trail no longer shows what was actually
    submitted — and the same stripping logic would eat `dose <5mg`.
    """
    stored, markers = prepare_content(payload)
    assert stored == payload
    assert isinstance(markers, list)


@SETTINGS
@given(
    prefix=st.sampled_from(CLINICAL_LITERALS),
    payload=st.sampled_from(INJECTION_LITERALS),
    suffix=st.sampled_from(CLINICAL_LITERALS),
)
def test_a_payload_next_to_clinical_prose_leaves_both_intact(prefix, payload, suffix):
    """Adjacency is where a stripping sanitiser does its damage: a tag-removal
    pass that spans from a `<` in the payload to a `>` in the prose deletes the
    clinical text between them."""
    text = f"{prefix} {payload} {suffix}"
    stored, _ = prepare_content(text)
    assert stored == text
    assert prefix in stored and suffix in stored


# ==========================================================================
# Markers are metadata; they never touch the content
# ==========================================================================


@SETTINGS
@given(text=st.text(max_size=300))
def test_markers_never_change_what_is_stored(text):
    """`prepare_content` returns `(stored, markers)`. Whatever the markers say,
    `stored` must equal `sanitize_for_storage(text)` — the detection is an
    observation about the content, not an edit to it."""
    stored, _ = prepare_content(text)
    assert stored == sanitize_for_storage(text)


@pytest.mark.parametrize("payload", INJECTION_LITERALS)
def test_something_worth_flagging_is_actually_flagged(payload):
    """The counterweight to every assertion above.

    All of those would pass if `find_injection_markers` returned `[]` for
    everything and the module did nothing at all. At least the unambiguous
    script and handler payloads must be noticed, so the audit trail records
    that someone submitted one.
    """
    if payload in NOT_APPLICABLE_TO_THIS_STACK:
        pytest.skip(f"not a threat in this stack — see NOT_APPLICABLE_TO_THIS_STACK")
    assert find_injection_markers(payload), f"nothing flagged for {payload!r}"


@pytest.mark.parametrize("clinical", CLINICAL_LITERALS)
def test_ordinary_clinical_prose_is_not_flagged_as_an_injection(clinical):
    """Markers are written to the audit log. If `BP <120/80` trips one, the log
    fills with false positives and the real signal stops being read — the same
    alert-fatigue failure as an over-eager risk flag."""
    assert find_injection_markers(clinical) == []


# ==========================================================================
# Escaping stays at the render boundary
# ==========================================================================


@pytest.mark.parametrize("clinical", CLINICAL_LITERALS)
def test_escape_html_is_available_but_not_applied_on_write(clinical):
    """`escape_html` exists for surfaces that genuinely emit HTML (PDF export,
    emailed summaries). Applying it on write is the double-escaping bug: the
    clinician sees `BP &lt;120/80` where they typed `BP <120/80`.

    Asserted as a pair — the function escapes when called, and the write path
    does not call it.
    """
    stored, _ = prepare_content(clinical)
    assert stored == clinical
    if "<" in clinical or "&" in clinical:
        assert escape_html(clinical) != clinical


# ==========================================================================
# End to end, through the real API
# ==========================================================================


@SETTINGS
@given(body=st.sampled_from(CLINICAL_LITERALS + INJECTION_LITERALS))
def test_what_the_api_returns_is_what_was_written(client_p1, token_for, body):
    """The round trip that actually matters, across the full write/read path.

    Pure-function tests above prove the sanitiser behaves. This proves nothing
    between the request and the response quietly re-escapes, strips or
    re-encodes on the way — serialisation and ORM layers are exactly where a
    second, forgotten transformation hides.
    """
    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    created = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "staff_note", "content": body},
    )
    assert created.status_code == 201, created.text
    assert created.json()["content"] == body

    fetched = client_p1.get(
        f"/entries/{created.json()['id']}", headers=headers
    )
    assert fetched.status_code == 200
    assert fetched.json()["content"] == body, (
        "content changed between write and read — something in the "
        "serialisation path is transforming it a second time"
    )
