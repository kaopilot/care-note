"""The human gate on patient-facing dosages.

The reviewers' hint: *"Patient facing generation is a higher severity class. You
show a patient something hallucinated and it's game over. Internal notes can get
audited, but what's sent to the patient needs more visible human approvals
and/or rules."*

`policy.py` already answers the generation half structurally — no machine-written
text can ever become patient-facing (D-067). So a wrong dosage in an instruction
was typed by a person, which is where the residual risk correctly sits, and a
second model reviewing it would add nothing.

What was missing was the *rules* half. This is it: before a patient-facing entry
is written or edited, any dose that is off by an order of magnitude has to be
acknowledged by the person writing it.

**Why acknowledgement rather than refusal.** A hard block on an out-of-range
figure would be wrong — specialist regimens exist, the reference table is small,
and a clinician who knows what they are doing must not be prevented from
recording it. Refusal would also teach people to route around the check. What it
does instead is make the clinician say, explicitly and in the audit trail, that
they meant it.

**Why only the implausible band.** `unusual` doses are common and legitimate.
Gating on those would produce a confirmation dialog on ordinary prescribing,
which is how a safety prompt becomes a reflex click — the same alert-fatigue
argument that shaped the learning floors (D-041).

See DECISIONS.md D-079.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from app.core.enums import EntryType
from app.security.policy import PATIENT_FACING_TYPES
from app.services import dosage


def assert_dosages_confirmed(
    *, entry_type: EntryType, content: str, confirmed: bool
) -> list[str]:
    """Raise 409 if patient-facing text carries an unconfirmed implausible dose.

    Returns the list of findings when they were confirmed, so the caller can
    record *what* was overridden in the audit log rather than merely that
    something was.
    """
    if entry_type not in PATIENT_FACING_TYPES:
        return []

    findings = dosage.blocking_findings(content)
    if not findings:
        return []

    if not confirmed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    "This is going to the patient and contains a dose far outside "
                    "the usual adult range. Check it against the source, then "
                    "confirm to continue."
                ),
                "reason": "dosage_needs_confirmation",
                "findings": [
                    {
                        "drug": f.drug,
                        "stated": f.stated,
                        "expected_low_mg": f.expected_low,
                        "expected_high_mg": f.expected_high,
                        "message": f.message,
                    }
                    for f in findings
                ],
            },
        )

    # Confirmed. The override is the thing worth recording — a gate nobody can
    # see the far side of is not a gate.
    return [f"{f.drug}:{f.stated}" for f in findings]
