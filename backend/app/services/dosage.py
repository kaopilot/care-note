"""Dosage plausibility — a reference check, not a prescribing engine.

The reviewers' capability list asks whether captured medication and dosage are
"confirmed through medical references and human confirmation". Before this, the
build extracted doses and compared them *against each other* (D-068), so it
could tell that two entries disagreed about a metformin dose and could not tell
that one of them said 5000mg.

That gap matters most exactly where the hint said it did: patient-facing content
is a higher severity class, and a transcription slip that moves a decimal point
is the realistic failure — not a wild hallucination.

**What this is.** A small table of adult single-dose ranges for the medications
already on the watchlist, with the outcome expressed as one of three states.

**What this is not.** Not a formulary, not a drug interaction checker, not
weight- or renal-adjusted, and not a prescribing authority. It cannot say a dose
is *correct* — only that it is outside a range where almost nothing legitimate
lives. Paediatric, oncology and specialist regimens sit outside its scope by
design, which is why an out-of-range figure produces a **question for a human**
rather than a blocked entry.

Three states, deliberately:

* ``plausible``   — inside the reference range. Says nothing about correctness.
* ``unusual``     — outside the range but within an order of magnitude. Worth
  a second look; plenty of legitimate prescribing lands here.
* ``implausible`` — off by an order of magnitude or more. This is the decimal
  slip, and it is the one that gets a hard human gate on patient-facing text.

See DECISIONS.md D-079.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.features import MEDICATIONS

PLAUSIBLE = "plausible"
UNUSUAL = "unusual"
IMPLAUSIBLE = "implausible"

# Adult single-dose ranges in milligrams. Sourced from ordinary adult
# prescribing ranges and rounded outward deliberately — a reference that is
# too tight generates false alarms, and a contradiction detector that cries
# wolf is worse than one with gaps (D-073).
#
# `None` for an upper bound means "not usefully bounded in mg" (insulin is
# dosed in units, so it is excluded rather than guessed at).
DOSE_RANGES_MG: dict[str, tuple[float, float] | None] = {
    "warfarin": (0.5, 15.0),
    "apixaban": (2.5, 10.0),
    "rivaroxaban": (2.5, 20.0),
    "clopidogrel": (75.0, 300.0),
    "aspirin": (75.0, 600.0),
    "metformin": (250.0, 1000.0),
    "gliclazide": (30.0, 160.0),
    "empagliflozin": (10.0, 25.0),
    "amlodipine": (2.5, 10.0),
    "lisinopril": (2.5, 40.0),
    "atorvastatin": (10.0, 80.0),
    "simvastatin": (10.0, 80.0),
    "amoxicillin": (250.0, 1000.0),
    "penicillin": (250.0, 1000.0),
    "prednisolone": (1.0, 60.0),
    "levothyroxine": (0.025, 0.3),  # dosed in micrograms; normalised to mg
    "insulin": None,  # units, not mass — excluded rather than guessed
}

_MASS_TO_MG = {"mcg": 0.001, "microgram": 0.001, "mg": 1.0, "g": 1000.0, "gram": 1000.0}

# Same shape as contradictions._DOSE so the two agree on what a dose looks like.
_DOSE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(mg|mcg|microgram|g|gram|units?|iu)\b", re.I)

# --------------------------------------------------------------------------
# Binding a dose to the drug it belongs to
# --------------------------------------------------------------------------
#
# This is shared with `services/contradictions.py`, and it is shared because the
# two had drifted into different answers for the same question. Contradictions
# took the FIRST dose in the sentence and gave it to EVERY drug named there, so
# an ordinary reconciliation line —
#
#     Continue metformin 1g BD, amlodipine 5mg OD, atorvastatin 20mg ON.
#
# — produced the claims "amlodipine 1g" and "atorvastatin 1g", and then reported
# a HIGH-severity dose disagreement against any later note that correctly said
# 5mg. This module was more careful and still wrong in the same direction: it
# searched a fixed 60-character window after the drug name, and that window ran
# straight through the next drug, so "metformin and amlodipine 5mg" read as
# metformin 5mg and tripped the patient-release gate.
#
# Both are the same defect: a window with no right-hand edge. The rule here is
# proximity with boundaries —
#
#   * look forward from the drug name, but never past the NEXT drug name;
#   * if nothing is there, look back a short way, but never past the PREVIOUS
#     drug name, and only accept a dose that is essentially adjacent
#     ("take 20mg atorvastatin", "20mg of atorvastatin").
#
# The backward pass is not just symmetry. Dose-before-drug is how instructions
# are actually written, and this module gates patient-facing text — so without
# it "take 200mg atorvastatin" carried no dose at all and the decimal slip the
# gate exists to catch went unseen. See DECISIONS.md D-101.

_DOSE_WINDOW_CHARS = 60
_DOSE_LOOKBACK_CHARS = 30
# How far a preceding dose may sit from the drug name it binds to. Enough for
# "20mg atorvastatin" and "20mg of atorvastatin", not enough to reach across a
# clause into an unrelated figure.
_DOSE_LOOKBACK_GAP = 6


@dataclass(frozen=True)
class DrugDose:
    """One medication mention, with the dose that belongs to it (or None)."""

    drug: str
    start: int  # offset of the drug name within the text it was found in
    end: int
    dose: tuple[str, str] | None  # (amount, unit) exactly as written


def drug_doses(text: str) -> list[DrugDose]:
    """Every watchlist medication in `text`, each bound to its nearest dose.

    Ordered by position. A drug with no dose nearby gets `dose=None` rather than
    inheriting someone else's — that is the whole point.
    """
    lowered = (text or "").lower()
    mentions: list[tuple[str, int, int]] = []
    for drug in MEDICATIONS:
        for match in re.finditer(rf"\b{re.escape(drug)}\b", lowered):
            mentions.append((drug, match.start(), match.end()))
    mentions.sort(key=lambda m: m[1])

    out: list[DrugDose] = []
    for index, (drug, start, end) in enumerate(mentions):
        next_start = mentions[index + 1][1] if index + 1 < len(mentions) else len(lowered)
        prev_end = mentions[index - 1][2] if index else 0

        # Forward, stopping at the next drug name.
        forward = lowered[end : min(end + _DOSE_WINDOW_CHARS, next_start)]
        hit = _DOSE.search(forward)
        dose: tuple[str, str] | None = None
        if hit:
            dose = (hit.group(1), hit.group(2).lower())
        else:
            # Backward, stopping at the previous drug name, and only if adjacent.
            low = max(prev_end, start - _DOSE_LOOKBACK_CHARS)
            behind = lowered[low:start]
            last = None
            for candidate in _DOSE.finditer(behind):
                last = candidate
            if last is not None and len(behind) - last.end() <= _DOSE_LOOKBACK_GAP:
                dose = (last.group(1), last.group(2).lower())

        out.append(DrugDose(drug=drug, start=start, end=end, dose=dose))
    return out


@dataclass(frozen=True)
class DoseFinding:
    drug: str
    amount_mg: float
    stated: str
    state: str
    expected_low: float
    expected_high: float

    @property
    def needs_human_confirmation(self) -> bool:
        """Only the order-of-magnitude case blocks. `unusual` informs."""
        return self.state == IMPLAUSIBLE

    @property
    def message(self) -> str:
        if self.state == IMPLAUSIBLE:
            return (
                f"{self.stated} of {self.drug} is far outside the usual adult range "
                f"({_fmt(self.expected_low)}–{_fmt(self.expected_high)} mg). Confirm "
                f"against the source before this is used."
            )
        return (
            f"{self.stated} of {self.drug} is outside the usual adult range "
            f"({_fmt(self.expected_low)}–{_fmt(self.expected_high)} mg). Worth checking."
        )


def _fmt(value: float) -> str:
    return f"{value:g}"


def _to_mg(amount: float, unit: str) -> float | None:
    factor = _MASS_TO_MG.get(unit.lower().rstrip("s"))
    return None if factor is None else amount * factor


def check_text(text: str) -> list[DoseFinding]:
    """Every drug-plus-dose in `text` whose figure falls outside its range.

    Returns findings only — a plausible dose produces nothing, because a card
    that annotates every correct dose trains people to ignore the annotation.
    """
    findings: list[DoseFinding] = []

    for mention in drug_doses(text):
        bounds = DOSE_RANGES_MG.get(mention.drug)
        if bounds is None or mention.dose is None:
            continue
        low, high = bounds
        amount, unit = mention.dose
        amount_mg = _to_mg(float(amount), unit)
        if amount_mg is None:
            continue  # units/IU — not a mass, nothing to compare
        if low <= amount_mg <= high:
            continue
        # The ranges above are *single-dose*. A legitimate daily total can
        # reach roughly three times a single dose (TDS), so anything beyond
        # 3x the upper bound exceeds any plausible daily total, let alone a
        # single administration — that is the decimal slip. An order of
        # magnitude was the first threshold tried and it was too lenient:
        # metformin 5000mg passed as merely "unusual", which is the exact
        # case the reviewers' hint describes.
        implausible = amount_mg > high * 3 or amount_mg < low / 10
        findings.append(
            DoseFinding(
                drug=mention.drug,
                amount_mg=amount_mg,
                stated=f"{amount}{unit}",
                state=IMPLAUSIBLE if implausible else UNUSUAL,
                expected_low=low,
                expected_high=high,
            )
        )
    return findings


def blocking_findings(text: str) -> list[DoseFinding]:
    """Findings severe enough to require a human decision before patient release."""
    return [f for f in check_text(text) if f.needs_human_confirmation]
