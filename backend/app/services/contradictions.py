"""Detecting clinical contradictions between entries — including human-human.

Why this exists
---------------
The conflict handling built in Phase 2 answers one question: what happens when a
clinician disagrees with an AI note. That is the question the brief asked, and
it is the easier one, because the resolution rule is given (clinician wins, and
the disagreement is recorded).

It is not the question that hurts people. **Clinicians and nurses contradict
each other**, in different notes, hours apart, and nobody is wrong on purpose.
A nurse records "allergic to penicillin"; a clinician, reading a different part
of a fragmented record, prescribes amoxicillin. A doctor writes "metformin 1g
BD"; a staff note two days later says "metformin 500mg BD". Neither note is AI
output, so no precedence rule applies, and neither author can see the
contradiction because the whole problem this product exists to solve is that
they are not reading the same page.

So this module looks for contradictions *between any two entries*, regardless of
who wrote them, and scopes itself to the three classes where being wrong is
worst:

* **Allergy vs administration** — an allergy recorded anywhere in the chart
  against a drug (or a drug in the same class) named as given or prescribed
  elsewhere. This is the one that kills people.
* **Dose disagreement** — the same medication carrying two different doses in
  two entries.
* **Status contradiction** — one entry saying a medication was started or
  continued, another saying the same drug was stopped or held.

What it deliberately is not
---------------------------
**It is extraction, not inference, and no model is involved.** Every finding
here comes from a regex or a vocabulary lookup over text that is already stored,
which means each one can be re-derived, points at the exact two entries that
produced it, and cannot drift between runs. A model asked "do these notes
contradict?" would be more sensitive and would also produce confident
disagreements that are not there — and a false contradiction on an allergy is
not a harmless false positive. It teaches a clinician that the flag means
nothing, which disarms it for the case that matters.

**It never resolves anything.** There is no precedence rule for human-human
contradiction and inventing one would be a clinical decision this system has no
standing to make. Deciding that the more recent note wins would silently
discard an allergy recorded last year in favour of a prescription written today.
Both entries are surfaced, both are linked, and a human decides. The system's
job here is to make the disagreement impossible to miss, not to settle it.

**Recall is honestly limited.** Detection rests on `features.MEDICATIONS`, a
watchlist rather than a formulary, and on dose expressions matching a numeric
pattern. A contradiction in vocabulary this module does not know is simply not
found — the failure mode is silence, never a wrong answer. That gap is stated in
`ARCHITECTURE.md` rather than papered over, because a clinician who believes
this catches everything is worse off than one who knows it catches three things
well.

See DECISIONS.md D-068.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.enums import AI_SCRIBED_TYPES, EntryType, RiskLevel
from app.core.provenance import entry_pointer
from app.models import Entry
from app.services.features import MEDICATIONS, NEGATION_RE

# Severity of each contradiction class. Allergy conflicts are `critical` and are
# never scored below it — see `NEVER_SOFTENED` in the Glance View integration.
ALLERGY_SEVERITY = RiskLevel.CRITICAL
DOSE_SEVERITY = RiskLevel.HIGH
STATUS_SEVERITY = RiskLevel.MEDIUM

# "allergic to penicillin", "penicillin allergy", "anaphylaxis to aspirin"
_ALLERGY_CUES = (
    r"allergic to",
    r"allergy to",
    r"anaphylaxis to",
    r"reaction to",
    r"intolerant of",
    r"intolerance to",
)

# Language that means the drug was actually given or prescribed, as opposed to
# merely discussed. "Do not give amoxicillin" must not read as administration,
# which is why negation is checked before this matches.
_ADMINISTRATION_CUES = (
    r"prescribed",
    r"started on",
    r"commenced",
    r"给",  # no-op for English text; present so a future locale has a home
    r"administered",
    r"given",
    r"dispensed",
    r"take",
    r"taking",
    r"continue",
)

_STOP_CUES = (r"stopped", r"discontinued", r"held", r"ceased", r"withheld", r"stop")
_START_CUES = (r"started", r"commenced", r"continue", r"continued", r"increased")

# Negation scope. Defined once in app.services.features and imported here so
# the two consumers cannot drift into disagreeing about whether the same
# sentence asserts something. Crude, and knowingly so: "denies", "no", "not"
# and "avoid" are the forms that actually appear in the corpus. Real negation
# scope detection is a documented gap (see the module docstring and D-068).
_NEGATION = NEGATION_RE

# A blanket denial: the patient says they have no allergies at all, rather than
# denying one specific drug. This is the form the scenario-13 case actually
# takes — a nurse records a penicillin allergy, the patient tells the AI she has
# no known allergies — and it was invisible because a denial was never a claim.
_BLANKET_DENIAL = re.compile(
    r"\b(?:nkda|nkma"
    r"|no\s+known\s+(?:drug\s+|medication\s+)?allerg(?:y|ies)"
    r"|nil\s+known\s+allerg(?:y|ies)"
    r"|deni(?:es|ed)\s+(?:any\s+|all\s+)?(?:known\s+)?allerg(?:y|ies)"
    r"|no\s+allerg(?:y|ies))\b(?!\s+to\b)",
    re.I,
)

# Stands in for "every drug" on the denial side of a blanket statement.
ANY_ALLERGEN = "*"

_DOSE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(mg|mcg|g|ml|units?|iu)\b", re.I)

# Mass units normalised to milligrams so "1g" and "1000mg" are recognised as the
# same dose. Without this the detector reports a contradiction between two
# entries that agree, which is the failure mode that trains people to ignore it.
# Volume and activity units are not inter-convertible with mass and are compared
# only against their own kind.
_MASS_TO_MG: dict[str, float] = {"mg": 1.0, "g": 1000.0, "mcg": 0.001}


def _normalise_dose(dose: tuple[str, str] | None) -> tuple[float, str] | None:
    """(amount, canonical_unit), or None. Mass collapses to mg; others stay."""
    if dose is None:
        return None
    amount, unit = dose
    try:
        value = float(amount)
    except ValueError:
        return None
    unit = unit.lower().rstrip("s")
    if unit in _MASS_TO_MG:
        return (round(value * _MASS_TO_MG[unit], 6), "mg")
    return (value, unit)


@dataclass(frozen=True)
class Contradiction:
    """One detected disagreement between two entries.

    Both sides are always populated. A contradiction with one entry is not a
    contradiction; it is an observation, and this module does not make those.
    """

    kind: str  # allergy_vs_administration | assertion_vs_denial | dose_disagreement | status_disagreement
    severity: RiskLevel
    subject: str  # the medication or allergen the two entries disagree about
    detail: str  # one line a clinician can read without opening either entry
    left_entry_id: str
    right_entry_id: str
    left_pointer: str
    right_pointer: str
    left_quote: str
    right_quote: str
    left_is_ai: bool
    right_is_ai: bool

    @property
    def human_human(self) -> bool:
        """True when neither side is machine-authored.

        Worth distinguishing: the AI-vs-human case already has a precedence
        rule, and this one does not.
        """
        return not self.left_is_ai and not self.right_is_ai


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+|\n", text or "") if s.strip()]


def _negated(sentence: str, position: int) -> bool:
    return bool(_NEGATION.search(sentence[:position]))


def _drug_mentions(sentence: str) -> list[tuple[str, int]]:
    """Every watchlist medication in a sentence, with its offset."""
    found: list[tuple[str, int]] = []
    lowered = sentence.lower()
    for drug in MEDICATIONS:
        for match in re.finditer(rf"\b{re.escape(drug)}\b", lowered):
            found.append((drug, match.start()))
    return found


def _matches_any(sentence: str, cues) -> bool:
    lowered = sentence.lower()
    return any(re.search(cue, lowered) for cue in cues)


@dataclass(frozen=True)
class _Claim:
    """One extracted assertion about one drug, anchored to its sentence."""

    drug: str
    drug_class: str
    sentence: str
    kind: str  # allergy | administration | start | stop
    dose: tuple[str, str] | None


def _extract_claims(text: str) -> list[_Claim]:
    claims: list[_Claim] = []
    for sentence in _sentences(text):
        for drug, position in _drug_mentions(sentence):
            if _negated(sentence, position):
                # A denial is not nothing. If the sentence is about allergy at
                # all, record it as a claim of its own kind so it can be
                # compared against an allergy asserted elsewhere in the chart.
                # Everything else negated stays dropped: "not started on
                # warfarin" contradicts nothing on its own.
                if _matches_any(sentence, _ALLERGY_CUES):
                    claims.append(
                        _Claim(drug, MEDICATIONS[drug], sentence.strip(), "allergy_denial", None)
                    )
                continue
            drug_class = MEDICATIONS[drug]
            dose_match = _DOSE.search(sentence)
            dose = (
                (dose_match.group(1), dose_match.group(2).lower()) if dose_match else None
            )

            if _matches_any(sentence, _ALLERGY_CUES):
                kind = "allergy"
            elif _matches_any(sentence, _STOP_CUES):
                kind = "stop"
            elif _matches_any(sentence, _ADMINISTRATION_CUES):
                kind = "administration"
            elif _matches_any(sentence, _START_CUES):
                kind = "start"
            elif dose is not None:
                # A drug written with a dose and no verb — "Metformin 1g BD" —
                # is how half of real notes record a medication. It is a claim
                # about the dose even though it claims nothing about the action.
                kind = "dose"
            else:
                continue
            claims.append(_Claim(drug, drug_class, sentence.strip(), kind, dose))

    # An allergy expressed without a watchlist drug name still matters —
    # "allergic to penicillin" where penicillin is not on the watchlist. Capture
    # the allergen as free text so it can be matched against later prose.
    # Negation is checked here too: the main loop skips a negated mention, and
    # without the same check this fallback would helpfully re-add it, turning
    # "patient denies allergy to aspirin" into a critical allergy conflict.
    for sentence in _sentences(text):
        for cue in _ALLERGY_CUES:
            match = re.search(rf"{cue}\s+([a-z][a-z\-]{{3,30}})", sentence, re.I)
            if not match:
                continue
            if _negated(sentence, match.start()):
                continue
            allergen = match.group(1).lower()
            if any(c.drug == allergen for c in claims):
                continue
            claims.append(_Claim(allergen, "", sentence.strip(), "allergy", None))

    # Blanket denials. Recorded once per text: "no known allergies" said twice
    # is still one position, and two copies would produce duplicate findings
    # against the same asserted allergy.
    for sentence in _sentences(text):
        if _BLANKET_DENIAL.search(sentence):
            claims.append(_Claim(ANY_ALLERGEN, "", sentence.strip(), "allergy_denial", None))
            break
    return claims


def _is_ai(entry: Entry) -> bool:
    return EntryType(entry.type) in AI_SCRIBED_TYPES


def _quote(sentence: str, limit: int = 140) -> str:
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def detect(entries: list[Entry]) -> list[Contradiction]:
    """Every contradiction between any two of these entries.

    Pairwise over entries, sentence-level within them. The chart sizes this runs
    against are tens of entries, so the quadratic term is not worth optimising
    away at the cost of being harder to read — and it runs on write, never on
    the Glance View read path.
    """
    claims_by_entry: list[tuple[Entry, list[_Claim]]] = [
        (entry, _extract_claims(entry.content or "")) for entry in entries
    ]
    out: list[Contradiction] = []
    seen: set[tuple] = set()

    for index, (left, left_claims) in enumerate(claims_by_entry):
        for right, right_claims in claims_by_entry[index + 1 :]:
            for lc in left_claims:
                for rc in right_claims:
                    finding = _compare(lc, rc)
                    if finding is None:
                        continue
                    kind, severity, subject, detail, flip = finding
                    a, b = (right, left) if flip else (left, right)
                    ac, bc = (rc, lc) if flip else (lc, rc)
                    key = (kind, subject, a.id, b.id)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(
                        Contradiction(
                            kind=kind,
                            severity=severity,
                            subject=subject,
                            detail=detail,
                            left_entry_id=a.id,
                            right_entry_id=b.id,
                            left_pointer=entry_pointer(a.id),
                            right_pointer=entry_pointer(b.id),
                            left_quote=_quote(ac.sentence),
                            right_quote=_quote(bc.sentence),
                            left_is_ai=_is_ai(a),
                            right_is_ai=_is_ai(b),
                        )
                    )

    # Most severe first; an allergy conflict must never sit below a dose one.
    order = {RiskLevel.CRITICAL: 0, RiskLevel.HIGH: 1, RiskLevel.MEDIUM: 2}
    out.sort(key=lambda c: order.get(c.severity, 3))
    return out


def _compare(left: _Claim, right: _Claim):
    """Compare two claims. Returns None, or (kind, severity, subject, detail, flip).

    `flip` puts the allergy side first when reporting, so the clinician reads
    "allergy recorded ... but given" rather than the reverse.
    """
    same_drug = left.drug == right.drug
    same_class = bool(left.drug_class) and left.drug_class == right.drug_class

    # 0. An allergy asserted in one entry and denied in another.
    #
    #    The disagreement *is* the signal. "Allergy recorded, patient denies it"
    #    means one of: the patient forgot, was never told, it was charted
    #    against the wrong record, or it was an intolerance rather than a true
    #    allergy — and a clinician needs to know which. Showing only the allergy
    #    is safe but wastes the one thing a longitudinal record was supposed to
    #    produce. Showing only the denial would be lethal.
    #
    #    HIGH, not CRITICAL: unlike allergy-vs-administration, nothing dangerous
    #    has happened yet — the safe action is already the one being taken. This
    #    is a reconciliation task, not an alarm, and rating it critical would
    #    dilute the level that means "someone is about to be given a drug they
    #    react to". See D-073.
    if left.kind == "allergy" and right.kind == "allergy_denial":
        if same_drug or right.drug == ANY_ALLERGEN:
            return _denial_finding(left, right, flip=False)
    if right.kind == "allergy" and left.kind == "allergy_denial":
        if same_drug or left.drug == ANY_ALLERGEN:
            return _denial_finding(right, left, flip=True)

    # A denial contradicts nothing except an assertion. Two denials agree, and a
    # denial against an administration is not a contradiction — it is a normal
    # prescription for a drug nobody claimed an allergy to.
    if "allergy_denial" in {left.kind, right.kind}:
        return None

    # 1. Allergy against administration — same drug, or same drug class.
    if left.kind == "allergy" and right.kind in {"administration", "start"}:
        if same_drug or same_class:
            return _allergy_finding(left, right, same_drug, flip=False)
    if right.kind == "allergy" and left.kind in {"administration", "start"}:
        if same_drug or same_class:
            return _allergy_finding(right, left, same_drug, flip=True)

    if not same_drug:
        return None

    # 2. Two different doses of the same drug. Compared on normalised units, and
    #    only between claims that are actually asserting a dose — an allergy
    #    sentence that happens to contain a number is not a prescription.
    if left.kind != "allergy" and right.kind != "allergy":
        left_dose = _normalise_dose(left.dose)
        right_dose = _normalise_dose(right.dose)
        if left_dose and right_dose and left_dose != right_dose:
            if left_dose[1] == right_dose[1]:
                return (
                    "dose_disagreement",
                    DOSE_SEVERITY,
                    left.drug,
                    f"{left.drug.capitalize()} recorded as "
                    f"{_render_dose(left.dose)} in one entry and "
                    f"{_render_dose(right.dose)} in another. Both are human-entered, "
                    f"so no precedence rule applies — a person has to decide which "
                    f"is current.",
                    False,
                )

    # 3. Started here, stopped there.
    if {left.kind, right.kind} == {"start", "stop"} or (
        {left.kind, right.kind} == {"administration", "stop"}
    ):
        stopped_first = left.kind == "stop"
        return (
            "status_disagreement",
            STATUS_SEVERITY,
            left.drug,
            f"{left.drug.capitalize()} is recorded as stopped in one entry and "
            f"as active in another.",
            stopped_first,
        )
    return None


def _render_dose(dose: tuple[str, str] | None) -> str:
    """As written in the note, not as normalised. A clinician checking the flag
    against the entry needs to find the same string they are being shown."""
    return f"{dose[0]}{dose[1]}" if dose else "an unstated dose"


def _denial_finding(allergy: _Claim, denial: _Claim, *, flip: bool):
    """An allergy recorded in one entry and denied in another.

    The assertion always reports first, so the clinician reads "allergy
    recorded ... but denied" rather than the reverse. Order matters here: the
    safe reading must lead.
    """
    if denial.drug == ANY_ALLERGEN:
        detail = (
            f"Allergy to {allergy.drug} is recorded in one entry, but another "
            f"entry records no known allergies. Confirm with the patient before "
            f"prescribing."
        )
    else:
        detail = (
            f"Allergy to {allergy.drug} is recorded in one entry and explicitly "
            f"denied in another. Confirm with the patient before prescribing."
        )
    return ("assertion_vs_denial", RiskLevel.HIGH, allergy.drug, detail, flip)


def _allergy_finding(allergy: _Claim, given: _Claim, same_drug: bool, *, flip: bool):
    if same_drug:
        detail = (
            f"Allergy to {allergy.drug} is recorded in one entry, and {given.drug} "
            f"is recorded as given or prescribed in another."
        )
    else:
        detail = (
            f"Allergy to {allergy.drug} is recorded in one entry, and {given.drug} — "
            f"same class ({given.drug_class}) — is recorded as given or prescribed "
            f"in another."
        )
    return ("allergy_vs_administration", ALLERGY_SEVERITY, allergy.drug, detail, flip)
