"""Clinical feature extraction — the vocabulary the Glance View scores on.

Two consumers, one extractor, on purpose:

* **Phase 2.4** scores highlight candidates using the tags produced here.
* **Phase 4** learns weights *per tag*. Learning only generalises if the tag
  emitted for "started warfarin" in March is the same string as the one emitted
  for "warfarin dose held" in August — so tags are normalised, lowercase, and
  namespaced (`med:warfarin`, `entity:allergy`, `action:referral`).

Keyword matching, not NLP, and that is a deliberate scope decision (see
DECISIONS.md D-029). A clinical NER model would have better recall on prose we
have not anticipated, but it would also be a black box in a product whose entire
thesis is that a clinician can see why something was surfaced. `risk_reason`
strings in the Glance View are generated straight from these tables, so every
suggestion the system makes can be traced to a line a reviewer can read.

The known cost: a medication not in `MEDICATIONS` scores as ordinary prose. That
is a recall gap, not a safety gap — an unrecognised term is simply not promoted,
and the entry still sits in the timeline where a human can read it. The failure
mode is "less helpful", never "silently hid something".
"""

from __future__ import annotations

import re

from app.core.enums import AI_SCRIBED_TYPES, EntryType, RiskLevel

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

# High-attention medications: narrow therapeutic index, common in polypharmacy
# incidents, or requiring monitoring. Not a formulary — a watchlist.
MEDICATIONS: dict[str, str] = {
    "warfarin": "anticoagulant",
    "apixaban": "anticoagulant",
    "rivaroxaban": "anticoagulant",
    "clopidogrel": "antiplatelet",
    "aspirin": "antiplatelet",
    "insulin": "glycaemic",
    "metformin": "glycaemic",
    "gliclazide": "glycaemic",
    "empagliflozin": "glycaemic",
    "semaglutide": "glycaemic",
    "amlodipine": "antihypertensive",
    "lisinopril": "antihypertensive",
    "losartan": "antihypertensive",
    "atenolol": "antihypertensive",
    "hydrochlorothiazide": "antihypertensive",
    "atorvastatin": "lipid",
    "simvastatin": "lipid",
    "prednisolone": "steroid",
    "salbutamol": "respiratory",
    "amoxicillin": "antibiotic",
    "penicillin": "antibiotic",
    "ibuprofen": "nsaid",
    "naproxen": "nsaid",
    "tramadol": "analgesic",
    "codeine": "analgesic",
    "sertraline": "psychotropic",
    "fluoxetine": "psychotropic",
    "levothyroxine": "endocrine",
}

# Terms that mean "someone could be harmed by what happens next".
RED_FLAG_TERMS: dict[str, str] = {
    "chest pain": "cardiac red flag",
    "shortness of breath": "respiratory red flag",
    "breathless": "respiratory red flag",
    "haemoptysis": "respiratory red flag",
    "bleeding": "bleeding risk",
    "bruising": "bleeding risk",
    "melaena": "bleeding risk",
    "syncope": "collapse risk",
    "fainted": "collapse risk",
    "collapse": "collapse risk",
    "suicidal": "safety risk",
    "self-harm": "safety risk",
    "sepsis": "sepsis concern",
    "febrile": "infection concern",
    "hypoglycaemia": "glycaemic emergency",
    "hypoglycemia": "glycaemic emergency",
    "anaphylaxis": "allergic emergency",
    "numbness": "neurological symptom",
    "paraesthesia": "neurological symptom",
    "tingling": "neurological symptom",
    "tingle": "neurological symptom",
    "weakness": "neurological symptom",
    "blurred vision": "neurological symptom",
    "fall": "falls risk",
    "falls": "falls risk",
    # Peripheral oedema. Added in Phase 5: it is one of the commonest adverse
    # drug effects in a primary-care population (calcium channel blockers), and
    # a patient describing it is describing the reason for the visit. Its
    # absence meant a consult whose entire clinical content was ankle swelling
    # produced a summary with no patient-reported section at all (D-052).
    "swelling": "oedema",
    "swollen": "oedema",
    "oedema": "oedema",
    "edema": "oedema",
}

ALLERGY_TERMS: tuple[str, ...] = ("allergy", "allergic", "anaphylaxis", "intolerance", "rash to")

# Chief-complaint framing — what the visit was actually about.
COMPLAINT_CUES: tuple[str, ...] = (
    "presents with",
    "presenting complaint",
    "chief complaint",
    "complains of",
    "reports",
    "came in for",
    "here for",
)

# Language that means work is outstanding. The Glance View's "open actions" row
# is made of these plus real Task rows.
ACTION_CUES: dict[str, str] = {
    "needs": "action requested",
    "arrange": "action requested",
    "refer": "referral",
    "referral": "referral",
    "order": "order pending",
    "repeat": "repeat test",
    "recheck": "repeat test",
    "review in": "review scheduled",
    "follow up": "follow-up",
    "follow-up": "follow-up",
    "chase": "result outstanding",
    "awaiting": "result outstanding",
    "pending": "result outstanding",
    "to be booked": "booking pending",
    "counsel": "counselling due",
    "titrate": "titration pending",
    "hold one dose": "medication change",
    # "start" and "stop" were bare verbs here until Phase 5's voice fixtures ran
    # through this vocabulary and put "before we start" and "When did it start?"
    # on the Glance View as pending medication changes. The bare forms match the
    # temporal sense of the word, which is by far the commoner one in speech —
    # transcripts are full of things starting and stopping that are not drugs.
    # A phantom open action costs more than a missed one here: the Top Card is
    # read in ten seconds and its authority depends on everything on it being
    # real. See DECISIONS.md D-051.
    "stop taking": "medication change",
    "stop the": "medication change",
    "start taking": "medication change",
    "start you on": "medication change",
    "started on": "medication change",
    "switch you to": "medication change",
    "switch to": "medication change",
}

# Hedging. Used to derive a confidence figure for offline summaries, and to
# down-weight a span that is speculation rather than finding.
UNCERTAINTY_TERMS: tuple[str, ...] = (
    "not sure",
    "unsure",
    "maybe",
    "possibly",
    "i think",
    "unclear",
    "query",
    "?",
    "cannot recall",
    "can't remember",
    "roughly",
    "approximately",
    "some kind of",
)

# Out-of-range numeric findings worth surfacing even without a keyword.
_VITALS_RE = re.compile(
    r"\b(?:bp|blood pressure)\s*:?\s*(\d{2,3})\s*/\s*(\d{2,3})\b", re.I
)
_HBA1C_RE = re.compile(r"\bhba1c\s*:?\s*(\d{1,2}(?:\.\d)?)\s*%?", re.I)
_INR_RE = re.compile(r"\binr\s*:?\s*(\d(?:\.\d)?)\b", re.I)

_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?", re.M)


# --------------------------------------------------------------------------
# Span splitting
# --------------------------------------------------------------------------


def sentences(text: str) -> list[tuple[int, int, str]]:
    """Split into `(start, end, text)` spans with offsets into the original.

    Offsets are the point: a highlight stores `span_start`/`span_end` and its
    provenance pointer carries them, so clicking through lands on the exact
    words rather than on the entry as a whole. Splitting in a way that loses
    offsets would make the pointer grammar in `core/provenance.py` unusable.
    """
    spans: list[tuple[int, int, str]] = []
    for match in _SENTENCE_RE.finditer(text or ""):
        raw = match.group()
        stripped = raw.strip()
        if len(stripped) < 8:  # fragments and stray punctuation carry no signal
            continue
        offset = match.start() + (len(raw) - len(raw.lstrip()))
        spans.append((offset, offset + len(stripped), stripped))
    return spans


# --------------------------------------------------------------------------
# Tagging
# --------------------------------------------------------------------------


def _numeric_flags(text: str) -> list[tuple[str, str]]:
    """Tags for out-of-range numbers. Thresholds are conventional adult primary
    care values, hard-coded because they are demo fixtures, not clinical policy.
    A real deployment would read these from a configurable rules table."""
    found: list[tuple[str, str]] = []

    vitals = _VITALS_RE.search(text)
    if vitals:
        systolic, diastolic = int(vitals.group(1)), int(vitals.group(2))
        if systolic >= 140 or diastolic >= 90:
            found.append(("finding:bp_elevated", f"BP {systolic}/{diastolic} above target"))

    hba1c = _HBA1C_RE.search(text)
    if hba1c and float(hba1c.group(1)) >= 7.0:
        found.append(("finding:hba1c_high", f"HbA1c {hba1c.group(1)}% above target"))

    inr = _INR_RE.search(text)
    if inr:
        value = float(inr.group(1))
        if value >= 3.0 or value <= 1.5:
            found.append(("finding:inr_out_of_range", f"INR {value} outside range"))

    return found


def tag_span(text: str) -> tuple[list[str], list[str]]:
    """Return `(feature_tags, human_reasons)` for one span of clinical prose.

    `human_reasons` is what the clinician reads as `risk_reason`. Keeping the
    two in lockstep here is what makes "why is this on my Glance View?"
    answerable without reading code.
    """
    lowered = text.lower()
    tags: list[str] = []
    reasons: list[str] = []

    for drug, drug_class in MEDICATIONS.items():
        if re.search(rf"\b{re.escape(drug)}\b", lowered):
            tags.append(f"med:{drug}")
            tags.append(f"medclass:{drug_class}")
            reasons.append(f"Medication mentioned: {drug}")

    for term, reason in RED_FLAG_TERMS.items():
        if term in lowered:
            tags.append(f"symptom:{term.replace(' ', '_')}")
            reasons.append(reason.capitalize())

    if any(term in lowered for term in ALLERGY_TERMS):
        tags.append("entity:allergy")
        reasons.append("Allergy or intolerance documented")

    if any(cue in lowered for cue in COMPLAINT_CUES):
        tags.append("entity:chief_complaint")

    for cue, reason in ACTION_CUES.items():
        if re.search(rf"\b{re.escape(cue)}\b", lowered):
            tags.append("entity:open_action")
            tags.append(f"action:{reason.replace(' ', '_').replace('-', '_')}")
            reasons.append(reason.capitalize())
            break

    for tag, reason in _numeric_flags(text):
        tags.append(tag)
        reasons.append(reason)

    if any(term in lowered for term in UNCERTAINTY_TERMS):
        tags.append("quality:hedged")

    # Stable order, no duplicates — tags are dictionary keys for learned
    # weights, so `["med:warfarin", "med:warfarin"]` must not double-count.
    return sorted(set(tags)), _dedupe(reasons)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def entry_level_tags(entry_type: str | EntryType, risk_level: str | RiskLevel) -> list[str]:
    """Tags describing the entry itself rather than its prose.

    `source:ai` vs `source:human` is here because Phase 4 should be able to
    learn that *this clinic* pays disproportionate attention to machine output —
    or ignores it. That is exactly the kind of calibration the brief is asking
    the system to notice.
    """
    tags = [f"type:{entry_type}"]
    if str(risk_level) != str(RiskLevel.NONE):
        tags.append(f"risk:{risk_level}")
    tags.append("source:ai" if EntryType(entry_type) in AI_SCRIBED_TYPES else "source:human")
    return tags


def uncertainty_ratio(text: str) -> float:
    """Fraction of sentences carrying hedging language, 0..1.

    Feeds the confidence figure attached to offline-generated summaries: a
    transcript full of "I think" and "maybe" should not produce a summary the
    UI presents with the same certainty as one full of measurements.
    """
    spans = sentences(text)
    if not spans:
        return 0.0
    hedged = sum(
        1 for _, _, span in spans if any(term in span.lower() for term in UNCERTAINTY_TERMS)
    )
    return hedged / len(spans)
