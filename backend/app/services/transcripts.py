"""Synthetic consult transcripts for the three AI-scribe interaction types.

EVERY LINE IN THIS FILE IS INVENTED. The names, NRIC numbers and phone numbers
below have never belonged to anyone. They are here *deliberately*: a transcript
fixture with no identifiers would let the scribe pipeline pass its tests without
the redaction chokepoint ever having anything to do, and a redaction boundary
nobody has watched work is a claim rather than a control.

So each template plants identifiers in the places real consult audio puts them —
the greeting, the callback number, the record lookup — and the pipeline's
`redaction_count` on the resulting `AIScribedNote` is the receipt that they were
removed before the text went anywhere near a model.

Transcripts are turn-structured rather than flat text because Phase 5's voice
capture produces the same shape: speaker label, timing, text. Writing the fixture
in the target shape now means the scribe service does not need rewriting when
real audio arrives — only the transcription source changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import InteractionType


@dataclass(frozen=True)
class Turn:
    speaker: str
    text: str
    start_ms: int
    end_ms: int
    confidence: float = 0.95
    language: str = "en"


@dataclass(frozen=True)
class TranscriptTemplate:
    interaction_type: InteractionType
    label: str
    description: str
    turns: tuple[Turn, ...]


def _fill(template: str, *, name: str, mrn: str, nric: str, phone: str) -> str:
    return (
        template.replace("{name}", name)
        .replace("{first}", name.split()[0])
        .replace("{mrn}", mrn)
        .replace("{nric}", nric)
        .replace("{phone}", phone)
    )


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------

_DOCTOR_CONSULT = TranscriptTemplate(
    interaction_type=InteractionType.DOCTOR_PATIENT_CONSULT,
    label="Doctor–patient consult",
    description="Diabetes review with a new neurological symptom.",
    turns=(
        Turn("clinician", "Good morning {name}, take a seat. Let me pull up your record — "
                          "that's {mrn}, NRIC {nric}?", 0, 5200),
        Turn("patient", "Yes doctor, that's right.", 5200, 6800),
        Turn("clinician", "Your HbA1c came back at 8.4%, up from 7.9 in April. How have you "
                          "been getting on with the metformin?", 6800, 14000),
        Turn("patient", "The morning one is fine. The evening one I miss maybe twice a week, "
                        "I'm usually still at work at that time.", 14000, 22500),
        Turn("clinician", "That's useful to know. Anything new since we last met?",
             22500, 26000),
        Turn("patient", "My feet have been tingling at night. Maybe two weeks now. Both feet.",
             26000, 32000),
        Turn("clinician", "Any numbness, or weakness in the legs?", 32000, 34800),
        # Romanised Hokkien, code-switched. Ordinary for an older patient in
        # this region, and the build has no vocabulary for it: this turn is
        # transcribed and stored faithfully, then produces no tags, no risk
        # level and no card. It is here so the "content I could not read" flag
        # fires on a real fixture rather than the gap being invisible in every
        # demo. See DECISIONS.md D-072.
        Turn("patient", "Bo numb lah. Ka joah tioh e kha there thiam thiam, "
                        "bo hoat tou khun.", 34800, 37000, 0.48, "nan"),
        Turn("patient", "Not numb exactly. Just the tingling.", 37000, 38000),
        Turn("clinician", "Given the glycaemic control, I want to screen for early diabetic "
                          "neuropathy. I'll also repeat your urine ACR — there was a query "
                          "about microalbuminuria last time.", 38000, 49000),
        Turn("clinician", "Let's keep the metformin dose as is for now and review titration "
                          "in three months. Keep your BP under 130 over 80.", 49000, 58000),
        Turn("patient", "Should I be worried about the tingling?", 58000, 61000),
        Turn("clinician", "It's a common early sign and it's the reason we screen. We'll know "
                          "more after the tests. If you can reach me, the clinic line is "
                          "{phone}.", 61000, 72000),
    ),
)

_NURSE_CONSULT = TranscriptTemplate(
    interaction_type=InteractionType.NURSE_PATIENT_CONSULT,
    label="Nurse–patient consult",
    description="Vitals, medication reconciliation and foot check.",
    turns=(
        Turn("staff", "Hi {first}, I'm the nurse today. Can you confirm your date of birth "
                      "and contact number for me?", 0, 6000),
        Turn("patient", "Born 11 March 1968. You can reach me on {phone}.", 6000, 12000),
        Turn("staff", "Thank you. Let's do your blood pressure. Sit back and relax your arm.",
             12000, 17000),
        Turn("staff", "BP is 138 over 86. I'll repeat it in a moment. Weight is 74.2 "
                      "kilograms, that's down about a kilo since March.", 17000, 27000),
        Turn("patient", "I've been walking in the evenings when I can.", 27000, 30500),
        Turn("staff", "That's helping. Repeat BP 134 over 84, a bit better.", 30500, 36000),
        Turn("staff", "Any allergies I should record? The file says penicillin.",
             36000, 40000),
        Turn("patient", "Yes, penicillin. I came out in a rash as a child.", 40000, 45000),
        Turn("staff", "Noted — penicillin allergy, rash. I'll do your foot check now. "
                      "Any tingling or numbness?", 45000, 52000),
        Turn("patient", "Tingling at night, yes. For about two weeks.", 52000, 56500),
        Turn("staff", "No ulceration and pulses are present, but I'll flag the tingling to "
                      "the doctor. Monofilament testing needs to be arranged.",
             56500, 66000),
    ),
)

_AI_PATIENT_SESSION = TranscriptTemplate(
    interaction_type=InteractionType.AI_PATIENT_SESSION,
    label="AI–patient session",
    description="Pre-consult intake conversation between the patient and the AI.",
    turns=(
        Turn("system", "Hello, I'm helping the clinic prepare for your appointment. "
                       "What would you like to talk about with the doctor?", 0, 6000),
        Turn("patient", "It's {name}. Mainly my feet. They tingle at night and it wakes me up "
                        "sometimes.", 6000, 14000),
        Turn("system", "How long has that been happening?", 14000, 16500),
        Turn("patient", "Maybe two weeks? I'm not sure exactly. It might have been longer, "
                        "I didn't really pay attention at first.", 16500, 25000),
        Turn("system", "Have you been able to take your medication as prescribed?",
             25000, 29000),
        Turn("patient", "The evening metformin is hard. I think I miss it a couple of times a "
                        "week. Possibly more, some weeks.", 29000, 38000),
        Turn("system", "Is there anything else worrying you?", 38000, 41000),
        Turn("patient", "My mother had diabetes and she had problems with her feet later on. "
                        "I suppose I'm worried about that.", 41000, 50000),
        Turn("system", "That's worth raising with the doctor. Anything about cost or getting "
                       "to appointments?", 50000, 56000),
        Turn("patient", "Taking time off is the hard part. If they call me it should be after "
                        "six, on {phone}.", 56000, 64000),
    ),
)

TEMPLATES: dict[InteractionType, TranscriptTemplate] = {
    InteractionType.DOCTOR_PATIENT_CONSULT: _DOCTOR_CONSULT,
    InteractionType.NURSE_PATIENT_CONSULT: _NURSE_CONSULT,
    InteractionType.AI_PATIENT_SESSION: _AI_PATIENT_SESSION,
}

# Synthetic identifiers injected into the templates. Format-valid so the
# redaction patterns are genuinely exercised; assigned to no one.
_SYNTHETIC_IDS: dict[str, tuple[str, str]] = {
    # patient_id -> (fake NRIC, fake phone)
    "patient-a1": ("S8412345D", "+65 6123 4567"),
    "patient-a2": ("S9134567B", "+65 6234 5678"),
    "patient-b1": ("S7598765C", "+65 6345 6789"),
    "patient-b2": ("S5911223A", "+65 6456 7890"),
}

DEFAULT_IDS = ("S8001234E", "+65 6000 1111")


def build_turns(
    interaction_type: InteractionType,
    *,
    patient_name: str,
    patient_mrn: str,
    patient_id: str | None = None,
) -> list[Turn]:
    """Materialise a template for one patient, identifiers and all."""
    template = TEMPLATES[interaction_type]
    nric, phone = _SYNTHETIC_IDS.get(patient_id or "", DEFAULT_IDS)
    return [
        Turn(
            speaker=turn.speaker,
            text=_fill(turn.text, name=patient_name, mrn=patient_mrn, nric=nric, phone=phone),
            start_ms=turn.start_ms,
            end_ms=turn.end_ms,
            confidence=turn.confidence,
            language=turn.language,
        )
        for turn in template.turns
    ]


def flatten(turns: list[Turn]) -> str:
    """Speaker-labelled plain text — what the summariser reads."""
    return "\n".join(f"{turn.speaker}: {turn.text}" for turn in turns)


def describe() -> list[dict]:
    """Catalogue for the UI's scribe panel."""
    return [
        {
            "interaction_type": str(template.interaction_type),
            "label": template.label,
            "description": template.description,
            "turn_count": len(template.turns),
        }
        for template in TEMPLATES.values()
    ]
