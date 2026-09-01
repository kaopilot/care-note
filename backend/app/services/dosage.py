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
    lowered = (text or "").lower()

    for drug in MEDICATIONS:
        bounds = DOSE_RANGES_MG.get(drug)
        if bounds is None:
            continue
        low, high = bounds
        for match in re.finditer(rf"\b{re.escape(drug)}\b", lowered):
            # Look only in the window after the drug name. A dose three
            # sentences away belongs to something else.
            window = lowered[match.end() : match.end() + 60]
            dose = _DOSE.search(window)
            if dose is None:
                continue
            amount_mg = _to_mg(float(dose.group(1)), dose.group(2))
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
                    drug=drug,
                    amount_mg=amount_mg,
                    stated=f"{dose.group(1)}{dose.group(2)}",
                    state=IMPLAUSIBLE if implausible else UNUSUAL,
                    expected_low=low,
                    expected_high=high,
                )
            )
    return findings


def blocking_findings(text: str) -> list[DoseFinding]:
    """Findings severe enough to require a human decision before patient release."""
    return [f for f in check_text(text) if f.needs_human_confirmation]
