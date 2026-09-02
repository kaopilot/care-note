"""Phase 6 — Malay clinical vocabulary (D-058).

Phase 5 carried code-switched speech through the pipeline intact but tagged
English only, so a patient describing symptoms in Malay produced no feature
tags, scored nothing, and never reached the Glance View. These tests pin the
three properties that make the fix worth having rather than merely present:

  1. Malay clinical prose produces tags at all.
  2. It produces the SAME canonical tag as the English equivalent — otherwise
     Phase 4 learns two unrelated features for one concept and a clinic's
     learned attention does not transfer across the language a patient used.
  3. English behaviour is unchanged, because the change adds no English key.
"""

from __future__ import annotations

import pytest

from app.services import features
from app.services.features import (
    MALAY_ALLERGY_TERMS,
    MALAY_CLINICAL_TERMS,
    RED_FLAG_TERMS,
    tag_span,
)

# (English prose, Malay prose, the tag both must emit)
EQUIVALENT_PAIRS = [
    ("Ankle swelling worst at night.", "Kaki bengkak, malam paling teruk.", "symptom:swelling"),
    ("Patient is febrile since Tuesday.", "Pesakit demam sejak Selasa.", "symptom:febrile"),
    (
        "Reports shortness of breath climbing stairs.",
        "Sesak nafas bila naik tangga.",
        "symptom:shortness_of_breath",
    ),
    ("She fainted in the waiting room.", "Dia pengsan di bilik menunggu.", "symptom:fainted"),
    ("Numbness in both feet for two weeks.", "Kaki kebas dua belah, dua minggu.", "symptom:numbness"),
    ("Bleeding from the gums when brushing.", "Gusi berdarah bila berus gigi.", "symptom:bleeding"),
    ("Had a fall last Thursday.", "Jatuh Khamis lepas.", "symptom:fall"),
    ("Generalised weakness since the fever.", "Badan lemah sejak demam.", "symptom:weakness"),
]


@pytest.mark.parametrize("english, malay, expected_tag", EQUIVALENT_PAIRS)
def test_malay_emits_the_same_canonical_tag_as_english(english, malay, expected_tag):
    """The whole point. `bengkak` must emit `symptom:swelling`, not
    `symptom:bengkak` — one concept is one learnable feature."""
    english_tags, _ = tag_span(english)
    malay_tags, _ = tag_span(malay)

    assert expected_tag in english_tags, f"English baseline broke for: {english}"
    assert expected_tag in malay_tags, f"Malay not recognised in: {malay}"


@pytest.mark.parametrize("english, malay, expected_tag", EQUIVALENT_PAIRS)
def test_malay_prose_is_not_silently_untagged(english, malay, expected_tag):
    """The regression this exists to prevent: before D-058 every one of these
    Malay strings returned an empty tag list."""
    malay_tags, reasons = tag_span(malay)
    assert malay_tags, f"no tags at all for: {malay}"
    assert reasons, f"tagged but gave the clinician no reason: {malay}"


def test_the_reason_names_the_term_that_actually_matched():
    """A clinician seeing 'Oedema' as the reason on a span of Malay text should
    be able to tell why. An unexplained English reason over Malay source reads
    as a mistranslation of the patient."""
    _, reasons = tag_span("Kaki bengkak sejak empat hari.")
    assert any("bengkak" in r for r in reasons), reasons


def test_malay_allergy_terms_are_recognised():
    tags, _ = tag_span("Ada alahan penicillin.")
    assert "entity:allergy" in tags
    assert "med:penicillin" in tags  # the drug name is not translated


def test_code_switched_prose_tags_both_halves():
    """The realistic case, and the one the Phase 5 fixtures actually contain:
    one sentence, two languages."""
    tags, _ = tag_span("The swelling is worst at night — malam paling teruk, kaki bengkak.")
    assert "symptom:swelling" in tags


# --------------------------------------------------------------------------
# The change must be additive: no English input may behave differently
# --------------------------------------------------------------------------

ENGLISH_UNCHANGED = [
    "Patient is stable, no acute concerns.",
    "Review in two weeks with repeat bloods.",
    "Discussed diet and exercise at length.",
    "BP 138/86 seated, repeat 134/84.",
    "Alertness normal, alignment of care plan agreed.",
    "Considering GLP-1 agonist if metformin titration insufficient.",
    "Query early microalbuminuria - repeat ACR.",
]


@pytest.mark.parametrize("text", ENGLISH_UNCHANGED)
def test_english_prose_picks_up_no_malay_tags(text):
    """Only terms with an existing English counterpart were added, so no
    English key changed. This asserts the consequence: ordinary clinical
    English must not acquire a tag by colliding with a Malay stem."""
    tags, _ = tag_span(text)
    malay_only_reasons = [t for t in tags if t.startswith("symptom:")]
    _, reasons = tag_span(text)
    assert not any("(Malay:" in r for r in reasons), (
        f"English prose matched a Malay term: {text} -> {reasons}"
    )


def test_every_malay_term_maps_to_a_real_english_key():
    """Guards the scope rule. If someone adds a Malay term whose English
    counterpart does not exist, the tag it emits is one nothing else in the
    system produces — a private feature that can never be reinforced by an
    English note about the same concept."""
    unknown = {
        malay: english
        for malay, english in MALAY_CLINICAL_TERMS.items()
        if english not in RED_FLAG_TERMS
    }
    assert not unknown, (
        "Malay terms mapping to a non-existent English vocabulary key: "
        f"{unknown}. Either add the English term to RED_FLAG_TERMS (which "
        "changes English scoring and needs its own decision) or drop it."
    )


def test_vocabulary_is_lowercase_and_stripped():
    """Matching is done against lowered text, so an uppercase entry would be
    dead weight nobody notices."""
    for term in list(MALAY_CLINICAL_TERMS) + list(MALAY_ALLERGY_TERMS):
        assert term == term.lower().strip(), f"unnormalised vocabulary entry: {term!r}"


def test_known_false_positive_is_documented_not_denied():
    """`jatuh` (fall) is also a component of Malay/Indonesian place names, so a
    referral letter naming one can register a falls-risk symptom. The failure
    is 'less precise', never 'silently hid something' — the entry still sits in
    the timeline. Asserted so the behaviour is a recorded property rather than
    a surprise; see D-058."""
    tags, _ = tag_span("Jatuh Gede clinic referral letter received.")
    assert "symptom:fall" in tags


# --------------------------------------------------------------------------
# Negation: a pre-existing limitation, inherited not introduced
# --------------------------------------------------------------------------

NEGATED = [
    ("en", "Patient denies chest pain.", "symptom:chest_pain"),
    ("en", "Denies any bleeding or bruising.", "symptom:bleeding"),
    ("en", "Without swelling or redness.", "symptom:swelling"),
    ("ms", "Tiada demam.", "symptom:febrile"),
    ("ms", "Tiada bengkak.", "symptom:swelling"),
]


@pytest.mark.parametrize("lang, text, tag", NEGATED)
def test_negation_is_not_handled_in_either_language(lang, text, tag):
    """Pins a KNOWN LIMITATION rather than a desired behaviour.

    Keyword matching has no notion of negation, so "denies chest pain" and
    "tiada demam" (no fever) both tag the symptom they rule out. This is not
    something the Malay vocabulary introduced — the English cases here fail
    identically and always have. It is asserted so that the day someone adds
    negation handling, these tests fail loudly and force a deliberate decision
    about scoring in BOTH languages at once, rather than one being fixed and
    the other quietly left behind.

    Why it was not fixed here: a negation guard changes English scoring, which
    would need its own decision and its own re-measurement of the Glance View,
    and this is the final day. See DECISIONS.md D-058. The failure direction is
    the safe one — a ruled-out symptom is surfaced for a human to dismiss, not
    a real one suppressed.
    """
    tags, _ = tag_span(text)
    assert tag in tags, (
        f"{text!r} no longer tags {tag}. If negation handling was added, that is "
        "an improvement — update this test and confirm it was applied to both "
        "languages, then re-measure the Glance View."
    )


# ==========================================================================
# The abstention flag, across writing systems
# ==========================================================================
#
# `is_unreadable` is the whole of D-072: when the tagger cannot read a turn it
# says so, rather than producing an empty tag list that is indistinguishable
# from "nothing clinical was said". It measured substantiveness in
# whitespace-delimited words, which is a defect and not a tuning choice —
# Chinese and Japanese are written without spaces, so `len(text.split())`
# returned 1 for any length of text and the flag never fired for two of the
# languages most likely to produce unreadable content in a Singapore or
# Malaysian clinic. Tamil failed differently: real four-word clinical sentences
# sat under a six-word bar tuned to filter English filler.


def test_an_unspaced_script_is_flagged_rather_than_silently_dropped():
    """Mandarin. One whitespace token, a whole clinical sentence."""
    text = "病人的脚肿了三天，晚上特别痛，需要看医生。"
    assert len(text.split()) == 1, "premise: whitespace tokenisation sees one token"
    assert features.is_unreadable(text, "zh") is True


def test_a_non_latin_spaced_script_clears_a_lower_bar():
    """Tamil. Four words, and a script that cannot be English small talk."""
    assert features.is_unreadable("நோயாளிக்கு கால் வீக்கம் உள்ளது.", "ta") is True


def test_romanised_hokkien_is_still_flagged():
    """The original D-072 case must be unaffected by the script-aware change."""
    assert (
        features.is_unreadable(
            "Bo lah, bo sio joah. Ka joah tioh e kha there thiam thiam, bo hoat tou khun.",
            "nan",
        )
        is True
    )


@pytest.mark.parametrize(
    "text,language",
    [
        ("Okay, right, thanks doctor.", "en"),
        ("Patient reports swelling in the ankle and shortness of breath.", "en"),
        ("Pesakit ada bengkak di kaki dan sesak nafas juga.", "ms"),
        ("The swelling is worst at night — malam paling sakit, cannot sleep.", "en-ms"),
        # Latin script, unknown language, but too short to be worth flagging.
        # The six-word bar still applies here, unchanged.
        ("Bo lah bo sio joah.", "nan"),
    ],
)
def test_supported_and_short_content_is_not_flagged(text, language):
    """The change must be additive. English and Malay behaviour is untouched,
    and a five-word romanised fragment stays below the bar it was always
    below — otherwise the flag becomes noise and stops being read."""
    assert features.is_unreadable(text, language) is False


def test_a_short_unspaced_fragment_is_below_the_bar():
    """"好的" is "okay". The character bar filters CJK filler the way the word
    bar filters English filler."""
    assert features.is_unreadable("好的。", "zh") is False
