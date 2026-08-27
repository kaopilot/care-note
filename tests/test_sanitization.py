"""Stored-XSS controls for multi-author free-text content.

The control ordering under test (see app/core/sanitization.py):
  1. untrusted content is never rendered as HTML  <- the actual defence
  2. write-time normalisation
  3. write-time detection, recorded as metadata

All payloads below are standard, publicly-documented XSS test strings.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.sanitization import (
    MAX_CONTENT_LENGTH,
    ContentTooLongError,
    escape_html,
    find_injection_markers,
    prepare_content,
    sanitize_for_storage,
)

FRONTEND_ROOT = Path(__file__).resolve().parents[1] / "frontend"
FRONTEND_SRC = FRONTEND_ROOT / "src"
# Phase 5 added the first shipped JavaScript living outside src/ — the service
# worker in public/. Scanning only src/ would have left it unchecked, so the
# scan follows the shipped code rather than one directory. Build output is
# excluded: it is generated from these sources, and it is gitignored.
FRONTEND_SCANNED = (FRONTEND_SRC, FRONTEND_ROOT / "public")


# --------------------------------------------------------------------------
# Control #1 — untrusted content is never rendered as HTML
# --------------------------------------------------------------------------


def test_frontend_never_renders_raw_html() -> None:
    """The primary XSS defence, asserted structurally.

    React escapes text children by default, so a stored `<script>` is inert —
    unless someone reaches for `dangerouslySetInnerHTML` or `innerHTML =`. This
    scan fails the build if they do, the same way the LLM chokepoint tests fail
    if a module reaches a model directly.
    """
    banned = re.compile(r"dangerouslySetInnerHTML|\.innerHTML\s*=|outerHTML\s*=")
    offenders = [
        str(path.relative_to(FRONTEND_ROOT))
        for root in FRONTEND_SCANNED
        if root.exists()
        for pattern in ("*.jsx", "*.js")
        for path in root.rglob(pattern)
        if banned.search(path.read_text())
    ]
    assert not offenders, (
        f"Raw HTML injection point(s) in frontend: {offenders}. Note and comment "
        "bodies are untrusted, multi-author content and must never be rendered "
        "as HTML."
    )


def test_markdown_renderer_not_added_without_review() -> None:
    """A Markdown renderer with HTML passthrough re-opens the hole control #1
    closes. If Phase 2 adds one, this test must be updated in the same commit
    that configures it with raw HTML disabled (DECISIONS.md D-015)."""
    package_json = FRONTEND_SRC.parent / "package.json"
    contents = package_json.read_text()
    for renderer in ("marked", "markdown-it", "react-markdown", "showdown"):
        assert renderer not in contents, (
            f"'{renderer}' was added without updating the XSS guard. Configure it "
            "with raw HTML disabled and amend this test."
        )


# --------------------------------------------------------------------------
# Control #2 — write-time normalisation, without corrupting clinical text
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "clinical_text",
    [
        "BP <120/80, well controlled.",
        "Reduce dose to <5mg daily.",
        "Sats <92% on room air — escalate.",
        "Weight >100kg; adjust dosing.",
        "Range 5<x<10 per protocol.",
    ],
)
def test_clinical_angle_brackets_survive_verbatim(clinical_text: str) -> None:
    """The reason we do not escape or strip on write.

    Escaping would store `BP &lt;120/80`, which React escapes AGAIN on render,
    showing the clinician a literal `&lt;`. Tag-stripping is worse: `<5mg` can
    be eaten entirely, silently turning a dose limit into `mg`. Silently
    altering clinical text is a patient-safety bug worse than the XSS it would
    be defending against — which control #1 has already neutralised.
    """
    assert sanitize_for_storage(clinical_text) == clinical_text


def test_control_characters_and_nulls_are_stripped() -> None:
    assert sanitize_for_storage("before\x00after") == "beforeafter"
    assert sanitize_for_storage("a\x08b\x1fc") == "abc"


def test_legitimate_whitespace_is_preserved() -> None:
    assert sanitize_for_storage("line one\nline two\tcolumn") == "line one\nline two\tcolumn"


def test_line_endings_are_normalised() -> None:
    assert sanitize_for_storage("a\r\nb\rc") == "a\nb\nc"


def test_unicode_is_nfc_normalised() -> None:
    """Decomposed and composed forms must compare equal, so lookups and diffs
    behave and homoglyph tricks are reduced."""
    decomposed = "e\u0301"  # e + combining acute
    assert sanitize_for_storage(decomposed) == "\u00e9"


def test_oversized_content_is_rejected() -> None:
    with pytest.raises(ContentTooLongError):
        sanitize_for_storage("a" * (MAX_CONTENT_LENGTH + 1))


def test_empty_input_is_safe() -> None:
    assert sanitize_for_storage("") == ""
    assert sanitize_for_storage(None) == ""


# --------------------------------------------------------------------------
# Control #3 — detection, recorded as metadata
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,marker",
    [
        ("<script>alert(1)</script>", "script_tag"),
        ("<SCRIPT SRC=//evil.test/x.js></SCRIPT>", "script_tag"),
        ("<iframe src=//evil.test></iframe>", "iframe_tag"),
        ("<img src=x onerror=alert(1)>", "event_handler"),
        ("<svg onload=alert(1)>", "svg_tag"),
        ("<a href='javascript:alert(1)'>click</a>", "javascript_url"),
        ("<embed src=evil.swf>", "object_embed"),
        ("data:text/html,<script>alert(1)</script>", "data_html_url"),
    ],
)
def test_injection_payloads_are_detected(payload: str, marker: str) -> None:
    assert marker in find_injection_markers(payload)


def test_clinical_text_produces_no_false_positives() -> None:
    assert find_injection_markers("BP <120/80 and dose <5mg. Patient stable.") == []
    assert find_injection_markers("Discussed the on-call roster with the team.") == []


def test_prepare_content_flags_without_rewriting() -> None:
    """The chokepoint returns the author's text unchanged plus the markers.

    Flagging rather than rejecting matters: a clinician quoting an error message
    that happens to contain `<script>` is writing a legitimate note.
    """
    payload = "Patient pasted this error: <script>alert(1)</script>"
    stored, markers = prepare_content(payload)
    assert stored == payload
    assert markers == ["script_tag"]


def test_prepare_content_clean_case() -> None:
    stored, markers = prepare_content("Start metformin 500mg BD. Review in 2 weeks.")
    assert markers == []
    assert "metformin" in stored


# --------------------------------------------------------------------------
# escape_html — for surfaces that genuinely emit HTML
# --------------------------------------------------------------------------


def test_escape_html_neutralises_payload() -> None:
    escaped = escape_html("<script>alert(1)</script>")
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped


def test_escape_html_escapes_quotes_for_attribute_context() -> None:
    assert '"' not in escape_html('x" onload="alert(1)')
