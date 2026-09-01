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

# --------------------------------------------------------------------------
# The high-risk floor
# --------------------------------------------------------------------------
# Canonical *tags*, not English strings. The floor used to be a tuple of English
# phrases matched against raw text, checked before `tag_span` ever ran — so the
# tagger spoke Malay and the safety floor did not. Measured, before the fix:
#
#     "chest pain when I walk uphill"        -> high
#     "sakit dada bila naik tangga"          -> medium
#
# The same symptom rated lower because of the language the patient used. Working
# in tag space means the floor inherits every language the tagger knows, now and
# whenever the vocabulary grows (D-072).
HIGH_RISK_TAGS: frozenset[str] = frozenset(
    {
        "symptom:chest_pain",
        "symptom:bleeding",
        "symptom:melaena",
        "symptom:haemoptysis",
        "symptom:syncope",
        "symptom:collapse",
        # `fainted` is clinically the same event as syncope and is the word
        # patients actually use. It was missing from the old English list, so
        # "she fainted" rated medium in English too — this was never only a
        # multilingual gap.
        "symptom:fainted",
        "symptom:suicidal",
        "symptom:self-harm",
        "symptom:anaphylaxis",
        "symptom:sepsis",
    }
)

# One definition, imported by app.services.contradictions. Two copies of a
# negation rule drift, and the two consumers would then disagree about whether
# the same sentence asserts something.
NEGATION_RE = re.compile(r"\b(no|not|never|deni(?:es|ed)|avoid|without|nil)\b[^.;]{0,40}$", re.I)

_SENTENCE_SPLIT = re.compile(r"(?<=[.;!?])\s+")

# Languages whose clinical vocabulary this build actually has. Anything else is
# transcribed and stored faithfully and then understood by nothing downstream.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"en", "ms", "en-ms", "ms-en"})

# Below this, a turn is "mm-hm" or "okay" and carries no clinical content, so an
# empty tag list means nothing is wrong.
_SUBSTANTIVE_WORDS = 6


def _term_to_high_risk_tag() -> dict[str, str]:
    """Every surface term, in any supported language, that maps to a high tag."""
    mapping: dict[str, str] = {}
    for term in RED_FLAG_TERMS:
        tag = f"symptom:{term.replace(' ', '_')}"
        if tag in HIGH_RISK_TAGS:
            mapping[term] = tag
    for malay_term, english_key in MALAY_CLINICAL_TERMS.items():
        tag = f"symptom:{english_key.replace(' ', '_')}"
        if tag in HIGH_RISK_TAGS:
            mapping[malay_term] = tag
    return mapping


def high_risk_tags(text: str) -> list[str]:
    """High-risk tags *asserted* in `text`, ignoring ones only ever denied.

    Negation handling is deliberately asymmetric. A single un-negated mention
    anywhere sets the floor, even if the same symptom is denied in ten other
    sentences: "no chest pain on Monday, chest pain today" must rate high. Only
    a symptom that appears exclusively inside a negation is dropped.

    Before this, the floor matched substrings with no negation handling at all,
    so a clean history — "denies chest pain, no shortness of breath" — rated
    high. That fails loud rather than silent, which is the right direction, but
    alert fatigue is the mechanism by which loud failures become silent ones.
    """
    found: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        lowered = sentence.lower()
        for term, tag in _term_to_high_risk_tag().items():
            position = lowered.find(term)
            if position < 0:
                continue
            if NEGATION_RE.search(sentence[:position]):
                continue
            if tag not in found:
                found.append(tag)
    return found


def is_unreadable(text: str, language: str | None) -> bool:
    """True when a turn carries clinical weight the vocabulary could not read.

    The failure this exists for: romanised Hokkien is transcribed faithfully,
    stored, and then produces no tags, no risk level, no highlight and no card.
    The words sit in the timeline where a human could read them, and the Glance
    View is silent about the reason for the visit — silent *confidently*, with
    nothing to indicate the tagger did not understand the language it was given.

    Abstention beats silence. Chasing recall with more vocabulary is an arms
    race; saying "there is content here I could not read" is not (D-072).

    Deliberately conservative: a turn must be substantive, produce no tags, AND
    be in a language outside the supported set. English small talk produces no
    tags either and is not a gap in understanding.
    """
    if not text or len(text.split()) < _SUBSTANTIVE_WORDS:
        return False
    if language and language.lower() in SUPPORTED_LANGUAGES:
        return False
    tags, _ = tag_span(text)
    return not tags

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

# --------------------------------------------------------------------------
# Malay clinical vocabulary (Phase 6)
# --------------------------------------------------------------------------
#
# Phase 5 carried code-switched speech through redaction, storage and
# summarisation intact and tagged it per segment — but `tag_span` read English
# only, so a patient describing symptoms in Malay produced no tags, scored
# nothing, and never reached the Glance View. In a Singapore or Malaysian clinic
# that is not an edge case, and it fails in the worst direction: the patients
# least likely to be understood in English are the ones the system quietly
# stops surfacing. See DECISIONS.md D-058.
#
# THE DESIGN POINT: each term maps to the **canonical English vocabulary key**,
# so `bengkak` emits `symptom:swelling` — the identical string `swelling` emits.
# Tags are the dictionary keys Phase 4 learns weights against, so emitting
# `symptom:bengkak` would create a second, separate feature and a clinic's
# learned attention would not transfer across the language its patients
# happened to use. One concept, one tag, whatever language it arrived in.
#
# SCOPE, deliberately narrow: only terms whose English counterpart is *already*
# in the tables above. This makes the change purely additive — no English input
# changes behaviour, because no English key is added or altered. Terms with no
# existing counterpart (`gatal` itchy, `muntah` vomiting, `cirit-birit`
# diarrhoea) are left out rather than added on both sides, which would be a
# scoring change to English prose smuggled in under a translation heading.
#
# NOT a translation layer. This is recall for a clinical watchlist. The system
# still stores and displays the original words verbatim — it never rewrites what
# a patient said into English.
MALAY_CLINICAL_TERMS: dict[str, str] = {
    # symptom → the English key in RED_FLAG_TERMS whose tag this reuses
    "bengkak": "swelling",
    "demam": "febrile",
    "sesak nafas": "shortness of breath",
    "semput": "breathless",
    "sakit dada": "chest pain",
    "berdarah": "bleeding",
    "pendarahan": "bleeding",
    "lebam": "bruising",
    "kebas": "numbness",
    "pengsan": "fainted",
    "pitam": "fainted",
    "jatuh": "fall",
    "lemah": "weakness",
    "kabur": "blurred vision",
}

# Allergy vocabulary is checked as a set rather than mapped, so it is listed
# separately. `alah` is the stem of `alahan` (allergy) and `alergi` (loan word).
MALAY_ALLERGY_TERMS: tuple[str, ...] = ("alah", "alergi", "alahan")

# Malay is the only non-English vocabulary here, because it is the one the
# Phase 5 capture fixtures actually contain (`en-ms`). Mandarin, Tamil and Hokkien
# are all common in the same clinics and are NOT covered — adding three more
# languages from the same generalist knowledge that produced this one would
# multiply an unreviewed risk rather than reduce a gap. See D-058 on why every
# term here still needs a native-speaker and clinical review before this is more
# than a demonstration that the mechanism works.

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

    # Malay terms emit the *English* key's tag, so one concept is one feature
    # regardless of the language it arrived in (D-058). The reason string names
    # the term that actually matched, because a clinician reading "Oedema" on
    # the Glance View should be able to see it came from the patient saying
    # "bengkak" — the provenance link lands on Malay text either way, and an
    # unexplained English reason over Malay source reads as a mistranslation.
    for malay_term, english_key in MALAY_CLINICAL_TERMS.items():
        if re.search(rf"\b{re.escape(malay_term)}\b", lowered):
            tags.append(f"symptom:{english_key.replace(' ', '_')}")
            reason = RED_FLAG_TERMS.get(english_key, english_key)
            reasons.append(f"{reason.capitalize()} (Malay: {malay_term})")

    if any(term in lowered for term in ALLERGY_TERMS):
        tags.append("entity:allergy")
        reasons.append("Allergy or intolerance documented")
    elif any(re.search(rf"\b{re.escape(t)}", lowered) for t in MALAY_ALLERGY_TERMS):
        tags.append("entity:allergy")
        reasons.append("Allergy or intolerance documented (Malay)")

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
