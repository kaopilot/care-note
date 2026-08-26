"""Content safety chokepoint for stored, multi-author, free-text content.

The threat is stored XSS: a `<script>` in a note or comment body, written by one
role, rendered later in another role's browser. This is a real risk profile for
this product — content is untrusted, multi-author, long-lived, and displayed
across privilege boundaries (a staff note surfaces in a clinician's Glance View).

Ordering of controls, strongest first
-------------------------------------
1. **Untrusted content is never rendered as HTML.** Note and comment bodies are
   plain text. React escapes text children by default, so the payload is inert.
   `tests/test_sanitization.py::test_frontend_never_renders_raw_html` scans the
   frontend and fails if `dangerouslySetInnerHTML` or `innerHTML =` appears
   anywhere — the same source-scanning technique that keeps the LLM chokepoint
   honest.
2. **Write-time normalisation** (`sanitize_for_storage`) strips control
   characters and caps length.
3. **Write-time detection** (`find_injection_markers`) records that a payload
   looked like an injection attempt, as metadata, without altering the text.

Why we do NOT strip or escape HTML on write
-------------------------------------------
This is a deliberate departure from the usual "sanitize before storage" advice,
and the reason is clinical rather than technical.

Clinical prose legitimately contains angle brackets: `BP <120/80`, `dose <5mg`,
`sats <92% on RA`. HTML-escaping on write would store `BP &lt;120/80`, which
React then escapes again on render, showing the clinician the literal string
`BP &lt;120/80`. Tag-stripping is worse: `<5mg` can be eaten entirely, silently
turning a dose limit into `mg`.

**Silently altering the text of a clinical note is a patient-safety bug, and a
worse one than the XSS it would be defending against** — because the XSS is
already neutralised by control #1, whereas a corrupted dose is not caught by
anything. So we store exactly what the author wrote, and put the escaping at the
render boundary where it belongs.

`escape_html()` exists here for any surface that genuinely emits HTML — a PDF
export, an emailed patient summary, a server-rendered page. Those surfaces must
call it. React surfaces must not.

Constraint on later phases
--------------------------
If Phase 2 introduces a Markdown renderer for note bodies, it MUST be configured
with raw HTML disabled (`marked`: `sanitize`/`html: false`; `markdown-it`:
`html: false`). A Markdown renderer with HTML passthrough re-opens exactly the
hole control #1 closes. Recorded in DECISIONS.md D-015.
"""

from __future__ import annotations

import html
import re
import unicodedata

# Generous enough for a long consult note, small enough to bound a payload.
MAX_CONTENT_LENGTH = 50_000

# Control characters that have no place in clinical prose. Tab, newline and
# carriage return are deliberately excluded — they are legitimate formatting.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Constructs that never legitimately appear in a clinical note. Used for
# DETECTION and flagging only — never to rewrite the author's text.
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("script_tag", re.compile(r"<\s*/?\s*script\b", re.I)),
    ("iframe_tag", re.compile(r"<\s*/?\s*iframe\b", re.I)),
    ("object_embed", re.compile(r"<\s*/?\s*(?:object|embed|applet)\b", re.I)),
    ("svg_tag", re.compile(r"<\s*svg\b", re.I)),
    ("event_handler", re.compile(r"\son(?:error|load|click|focus|mouseover|"
                                 r"animationstart|toggle|begin)\s*=", re.I)),
    ("javascript_url", re.compile(r"javascript\s*:", re.I)),
    ("data_html_url", re.compile(r"data\s*:\s*text/html", re.I)),
    ("style_expression", re.compile(r"expression\s*\(", re.I)),
)


class ContentTooLongError(ValueError):
    """Raised when submitted content exceeds MAX_CONTENT_LENGTH."""


def find_injection_markers(text: str | None) -> list[str]:
    """Return the names of injection constructs present in `text`.

    Detection, not prevention. A non-empty result means "flag and log this",
    not "reject" — a clinician quoting an error message that happens to contain
    `<script>` is writing a legitimate note.
    """
    if not text:
        return []
    return [name for name, pattern in INJECTION_PATTERNS if pattern.search(text)]


def sanitize_for_storage(text: str | None, *, max_length: int = MAX_CONTENT_LENGTH) -> str:
    """Normalise author-supplied content for storage.

    Deliberately conservative: strips control characters and NUL bytes,
    normalises Unicode to NFC (so visually identical strings compare equal and
    homoglyph tricks are reduced), normalises line endings, and enforces a
    length cap. The author's words themselves are returned unchanged.
    """
    if not text:
        return ""

    normalised = unicodedata.normalize("NFC", text)
    normalised = normalised.replace("\r\n", "\n").replace("\r", "\n")
    normalised = _CONTROL_CHARS.sub("", normalised)

    if len(normalised) > max_length:
        raise ContentTooLongError(
            f"content is {len(normalised)} characters; maximum is {max_length}"
        )
    return normalised


def escape_html(text: str | None) -> str:
    """Escape for a surface that actually emits HTML (PDF export, email).

    React surfaces must NOT call this — React escapes text children already, and
    escaping twice shows the user literal `&lt;` where they wrote `<`.
    """
    return html.escape(text or "", quote=True)


def prepare_content(text: str | None) -> tuple[str, list[str]]:
    """The chokepoint every write path should call.

    Returns `(stored_text, injection_markers)`. Callers persist `stored_text`
    verbatim and record the markers as metadata — never the content — so an
    attempted injection is visible in the audit trail without the note being
    silently rewritten.
    """
    cleaned = sanitize_for_storage(text)
    return cleaned, find_injection_markers(cleaned)
