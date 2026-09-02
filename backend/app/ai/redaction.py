"""PHI redaction chokepoint.

Contract (from the candidate brief): names, IC/ID numbers and phone numbers are
stripped BEFORE any text leaves for an LLM. `redact_phi(text) -> str` is the
single function that does it, and `llm_client` is the single caller that can
reach the network — so there is exactly one path out and it always runs this.

Approach and its honest limits
------------------------------
Deterministic regex + a name gazetteer, deliberately, not an NER model:

* The data is synthetic, so recall against real-world name diversity is not the
  thing being tested here — the presence and un-bypassability of the boundary is.
* A regex pass is auditable line by line. A reviewer can read this file and know
  what does and does not leave. That is worth more in a trust system than a few
  points of F1 from a model nobody can inspect.
* It is fast enough to sit on the hot path without a latency budget argument.

Known gaps, stated rather than hidden (see ARCHITECTURE.md "Redaction limits"):
lowercase names in running prose, unusual/transliterated names absent from the
gazetteer, and identification by rare-condition + date combinations are all out
of reach for this approach. A production build would layer a clinical NER pass
(e.g. Presidio or scispaCy) behind the same `redact_phi` signature, so nothing
downstream would change.

Redaction is CONSISTENT within a call: the same name maps to the same
placeholder every time, so "Mr Tan said Tan's cough worsened" still reads
coherently to the LLM as one person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Detector patterns
# --------------------------------------------------------------------------

# Singapore NRIC/FIN: S/T/F/G/M + 7 digits + checksum letter.
NRIC_RE = re.compile(r"\b[STFGM]\d{7}[A-Z]\b")
# Malaysian IC: YYMMDD-PB-###G
MYKAD_RE = re.compile(r"\b\d{6}-\d{2}-\d{4}\b")
# Medical record numbers / generic patient identifiers.
MRN_RE = re.compile(r"\b(?:MRN|NRIC|IC|ID|Patient\s*ID)[:\s#-]*([A-Z0-9][A-Z0-9-]{3,})\b", re.I)

# Phones: +65 xxxx xxxx, +60 xx-xxx xxxx, local 8-digit SG, generic international.
PHONE_RE = re.compile(
    r"(?:\+?\b(?:65|60|1)[-.\s]?)?"
    r"\b(?:\(\d{2,4}\)[-.\s]?)?"
    r"\d{3,4}[-.\s]?\d{3,4}(?:[-.\s]?\d{3,4})?\b"
)
# Restrictive phone pattern used first; the loose one above over-matches, so it
# only runs on candidates that carry a phone cue word or a leading +.
PHONE_CUE_RE = re.compile(
    r"(?:(?:tel|phone|mobile|hp|contact|call(?:ed|s)?(?:\s+(?:me|her|him|them))?|reach(?:able)?)"
    r"\s*(?:at|on|:)?\s*)(\+?[\d][\d\s().-]{6,17}\d)",
    re.I,
)
INTL_PHONE_RE = re.compile(r"\+\d{1,3}[\s.-]?\d[\d\s().-]{5,16}\d")
SG_LOCAL_PHONE_RE = re.compile(r"\b[89]\d{3}[\s-]?\d{4}\b")

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Dates of birth carry identification risk in a way that a consult date does not.
DOB_RE = re.compile(
    r"\b(?:DOB|D\.O\.B\.|date of birth|born(?:\s+on)?)\s*[:\-]?\s*"
    r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\s+\w+\s+\d{4})",
    re.I,
)

# Names introduced by an honorific or a role label. Captures multi-word names.
#
# Separators are [ \t] rather than \s throughout: \s matches newlines, which let
# a match run off the end of one line and swallow the start of the next —
# "Patient: Daniel Choo\nSeen by: Grace Tan" was matching "Daniel Choo Seen" as
# one name and leaving the second person exposed. Names do not span lines.
_SP = r"[ \t]"
_NAME_WORD = r"[A-Z][a-z'’-]+"
_CONNECTOR = r"(?:bin|binti|a/l|a/p|s/o|d/o|van|de|del|al)"

HONORIFIC_RE = re.compile(
    rf"\b(?:Dr|Doctor|Mr|Mrs|Ms|Miss|Mdm|Madam|Prof|Sr|Nurse|Sister|Encik|Puan|Cik)\.?{_SP}+"
    rf"({_NAME_WORD}(?:{_SP}+{_CONNECTOR}{_SP}+{_NAME_WORD}|{_SP}+{_NAME_WORD}){{0,3}})"
)
# "Patient: Amira Rahman" / "Seen by: Wei Ling Chua"
LABELLED_NAME_RE = re.compile(
    r"\b(?:Patient|Pt|Client|Caregiver|Next of kin|NOK|Seen by|Attending|Clinician|Nurse|Staff)"
    rf"{_SP}*[:\-]{_SP}*({_NAME_WORD}(?:{_SP}+{_NAME_WORD}){{0,3}})"
)

PLACEHOLDERS = {
    "name": "[NAME_{n}]",
    "nric": "[ID_{n}]",
    "phone": "[PHONE_{n}]",
    "email": "[EMAIL_{n}]",
    "dob": "[DOB]",
    "mrn": "[ID_{n}]",
}

# Words that look like names to a title-case heuristic but are not. Kept small
# and explicit; the gazetteer path is the primary name detector.
_NON_NAME_TOKENS = {
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
    "Clinic", "Hospital", "Ward", "Pharmacy", "Panadol", "Metformin", "Warfarin",
    "Amlodipine", "Atorvastatin", "Lisinopril", "Insulin", "Paracetamol",
    "Diabetes", "Hypertension", "Asthma", "COPD", "Type", "Blood", "Pressure",
}


# Titles and connectors that appear INSIDE a stored display name ("Dr Lim",
# "Nurse Priya", "Nurul binti Hassan"). When a full name is split into its
# parts for matching, these are not parts — redacting the word "Nurse" would
# mangle every sentence in a nurse consult.
_NAME_AFFIXES = {
    "dr", "doctor", "mr", "mrs", "ms", "miss", "mdm", "madam", "prof", "sr",
    "nurse", "sister", "encik", "puan", "cik",
    "bin", "binti", "a/l", "a/p", "s/o", "d/o", "van", "de", "del", "al",
}


def expand_name_parts(names: set[str]) -> set[str]:
    """Full names plus the individual parts people are actually called by.

    A gazetteer of display names alone only ever matches the formal form. Real
    consult speech says "Hi Amira" and "Rahman is here for his review", and
    those are the mentions a name detector most needs to catch — the formal
    "Amira Rahman" is also the one the honorific and label patterns already
    handle. Without this expansion the gazetteer's whole reason for existing
    goes unmet, which is precisely the defect Phase 5 found (DECISIONS.md
    D-050).

    Titles and connectors are excluded, and so is anything already known not to
    be a name, so "Dr Lim" contributes "Lim" and not "Dr".
    """
    expanded: set[str] = set()
    for name in names:
        if not name:
            continue
        expanded.add(name)
        for part in re.split(r"[\s,]+", name):
            token = part.strip(".'’-")
            if len(token) < 2:
                continue
            if token.lower() in _NAME_AFFIXES:
                continue
            if token in _NON_NAME_TOKENS:
                continue
            expanded.add(token)
    return expanded


@dataclass
class RedactionResult:
    """Full accounting of one redaction pass — used for audit and tests."""

    text: str
    replacements: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    # Placeholder -> category. Deliberately does NOT retain the original value:
    # a reversible map would be a second copy of the PHI.
    placeholders: dict[str, str] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.replacements == 0


class _Redactor:
    def __init__(self, gazetteer: set[str] | None = None) -> None:
        # Expanded HERE rather than at each call site. Every caller that knows
        # the names in scope gets bare-first-name matching automatically, and
        # no future caller can forget to ask for it.
        self.gazetteer = expand_name_parts({g for g in (gazetteer or set()) if g})
        self._counters: dict[str, int] = {}
        self._seen: dict[str, str] = {}  # original value -> placeholder (per pass)
        self.by_category: dict[str, int] = {}
        self.placeholders: dict[str, str] = {}
        self.replacements = 0

    def _placeholder(self, category: str, value: str) -> str:
        key = f"{category}:{value.strip().lower()}"
        if key in self._seen:
            return self._seen[key]
        # Counters are keyed on the TOKEN TEMPLATE, not the category, because
        # two categories share one template: `nric` and `mrn` both render as
        # `[ID_{n}]`. Counting per category made the first NRIC and the first
        # MRN in a document both come out as `[ID_1]` — two different
        # identifiers collapsed into one, which would tell a downstream model
        # that a record number and an NRIC were the same thing. Found while
        # building the Phase 2 scribe pipeline against a transcript containing
        # both; see DECISIONS.md D-034.
        template = PLACEHOLDERS[category]
        self._counters[template] = self._counters.get(template, 0) + 1
        token = template.format(n=self._counters[template])
        self._seen[key] = token
        self.placeholders[token] = category
        return token

    def _sub(self, pattern: re.Pattern[str], text: str, category: str, group: int = 0) -> str:
        def repl(match: re.Match[str]) -> str:
            value = match.group(group)
            if not value or not value.strip():
                return match.group(0)
            token = self._placeholder(category, value)
            self.replacements += 1
            self.by_category[category] = self.by_category.get(category, 0) + 1
            if group == 0:
                return token
            # Preserve the label ("Tel: ") and replace only the captured value.
            whole = match.group(0)
            start = match.start(group) - match.start(0)
            end = match.end(group) - match.start(0)
            return whole[:start] + token + whole[end:]

        return pattern.sub(repl, text)

    def run(self, text: str) -> str:
        if not text:
            return text

        # Fold typographic separators to ASCII before anything is matched.
        #
        # Every pattern here spells its separator class as ASCII — `[-.\s]`,
        # `[\s-]`, `[\d\s().-]`. `\s` is Unicode-aware in Python 3, so a
        # non-breaking or ideographic space costs nothing. The dash class is
        # not: an en-dash, em-dash, figure dash or non-breaking hyphen defeats
        # every phone pattern *and* `find_residual_phi`, so "hp 9123–4567"
        # passed through untouched with a clean tripwire.
        #
        # That is not an exotic input. iOS and macOS autocorrect a hyphen
        # between digits into an en-dash, and so does pasting from Word or
        # Google Docs — and this build's whole premise is text arriving from
        # phones and transcripts rather than from a typed EHR field.
        #
        # Folding beats widening each pattern: there is one place to add a
        # character, rather than six regexes that must agree forever. See
        # DECISIONS.md D-095.
        text = _fold_separators(text)

        # Order matters. Structured identifiers first — they are unambiguous and
        # removing them stops the looser name/phone passes from tripping on them.
        text = self._sub(EMAIL_RE, text, "email")
        text = self._sub(NRIC_RE, text, "nric")
        text = self._sub(MYKAD_RE, text, "nric")
        text = self._sub(MRN_RE, text, "mrn", group=1)
        text = self._sub(DOB_RE, text, "dob", group=1)

        # Phones: cue-anchored, then explicit international, then SG local.
        text = self._sub(PHONE_CUE_RE, text, "phone", group=1)
        text = self._sub(INTL_PHONE_RE, text, "phone")
        text = self._sub(SG_LOCAL_PHONE_RE, text, "phone")

        # Names: honorific/label-anchored patterns, then the gazetteer.
        text = self._sub(HONORIFIC_RE, text, "name", group=1)
        text = self._sub(LABELLED_NAME_RE, text, "name", group=1)
        text = self._redact_gazetteer(text)
        return text

    def _redact_gazetteer(self, text: str) -> str:
        """Redact known synthetic names supplied by the caller (seeded users and
        patients). Longest-first so full names beat their component parts."""
        if not self.gazetteer:
            return text
        terms = sorted(self.gazetteer, key=len, reverse=True)
        for term in terms:
            if term in _NON_NAME_TOKENS or len(term) < 2:
                continue
            pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
            text = self._sub(pattern, text, "name")
        return text


# Typographic dashes that a phone number can arrive wearing. Folded to ASCII
# "-" before matching so every separator class in this module only has to spell
# one character. Deliberately dashes only: `\s` already covers exotic spaces,
# and folding anything wider risks rewriting clinical prose.
_DASH_CHARS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"
_DASH_RE = re.compile(f"[{_DASH_CHARS}]")


def _fold_separators(text: str) -> str:
    """Normalise typographic dashes to ASCII for matching.

    Applied to the copy that goes to the model, not to what is stored: the
    transcript and the Entry keep the author's original characters, and this
    output is LLM input. So the cost is a hyphen shape in a prompt, and the
    benefit is that a phone number cannot hide behind an en-dash.
    """
    return _DASH_RE.sub("-", text or "")


def redact_phi(text: str) -> str:
    """The chokepoint. Strip names, IC/ID numbers and phone numbers from `text`.

    This is the signature the brief specifies and the one all production code
    should call. Use `redact_phi_detailed` when you need the audit counts.

    Falsy input (None, "") returns "" rather than propagating None — a
    redaction function should never hand a caller something un-redactable.
    """
    return redact_phi_detailed(text).text


def redact_phi_detailed(text: str, *, gazetteer: set[str] | None = None) -> RedactionResult:
    """Same pass as `redact_phi`, returning full accounting.

    `gazetteer` lets a caller add known names (seeded patients/users for this
    clinic) so bare first-name mentions in prose are caught too.
    """
    redactor = _Redactor(gazetteer=gazetteer)
    redacted = redactor.run(text or "")
    return RedactionResult(
        text=redacted,
        replacements=redactor.replacements,
        by_category=redactor.by_category,
        placeholders=redactor.placeholders,
    )


# Patterns re-checked AFTER redaction as a fail-closed tripwire in llm_client.
# Only the unambiguous ones — a false positive here blocks a legitimate call.
RESIDUAL_PHI_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("nric", NRIC_RE),
    ("mykad", MYKAD_RE),
    ("email", EMAIL_RE),
    ("intl_phone", INTL_PHONE_RE),
    ("sg_phone", SG_LOCAL_PHONE_RE),
)


def find_residual_phi(text: str) -> list[str]:
    """Return the categories of any unambiguous PHI still present.

    Folds separators first, for the same reason `run` does — and for one more.
    This function is the tripwire *and* the oracle the property tests assert
    against, so any pattern gap it shares with the redactor is invisible twice:
    the redactor misses it, and the test that would have caught the miss uses
    the same regex to look. That is exactly how "hp 9123–4567" passed with a
    clean residual report (D-095). Folding here is what stops the tripwire
    inheriting the redactor's blind spots.
    """
    folded = _fold_separators(text)
    return [name for name, pattern in RESIDUAL_PHI_PATTERNS if pattern.search(folded)]
