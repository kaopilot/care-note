"""The analysers, driven by text nobody wrote on purpose.

Everything in this file is reached by one path: a transcript. A transcript is
whatever a microphone, an ASR pass and a network hop between them produced —
mojibake, half a script, a control character, one word repeated four hundred
times, an empty string where a speaker said nothing audible.

None of that is hypothetical for this build. It is the input the ambient
capture feature exists to accept.

An exception in any of these functions is not a stack trace someone reads
later. `glance` calls them to build the Top Card, so it is a **blank card while
a clinician stands next to a patient** — the same failure as a model timeout,
arriving from a code path nobody thought could produce one.

Two properties throughout:

* **Totality** — never raise, for any input.
* **Determinism** — the same input gives the same answer, every time. A risk
  badge that changes between two page loads of an unchanged chart is worse than
  no badge, because the clinician cannot tell whether the chart changed or the
  system is guessing. The 48-hour hint asked exactly this: "how would we know
  if it were wrong?" An unstable score cannot be wrong, because it never says
  the same thing twice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.enums import Role
from app.services import contradictions, dosage, features

SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# Text drawn from the whole unicode range, not just Latin. The abstention flag
# was broken for two years' worth of the region's languages because every test
# fixture was romanised (D-090); a generator that only emits ASCII would have
# reproduced that blind spot faithfully.
any_text = st.text(max_size=600)

clinical_ish = st.lists(
    st.sampled_from(
        [
            "patient", "allergic", "to", "penicillin", "amoxicillin", "500mg",
            "metformin", "1g", "BD", "TDS", "stopped", "started", "no", "known",
            "allergies", "denies", "review", "in", "two", "weeks", "sorry",
            "correction", "make", "that", "250mg", "warfarin", "INR", "3.4",
            "bengkak", "kaki", "tiada", "demam", "。", "，", "、",
        ]
    ),
    min_size=0,
    max_size=60,
).map(" ".join)


class _Entry:
    def __init__(self, entry_id: str, content: str, *, ai: bool = True) -> None:
        self.id = entry_id
        self.type = "ai_doctor_consult_summary" if ai else "staff_note"
        self.content = content
        self.author_role = Role.SYSTEM if ai else Role.STAFF
        self.title = ""
        self.version_number = 1
        self.timestamp = datetime.now(timezone.utc)


# ==========================================================================
# Totality — nothing raises
# ==========================================================================


@SETTINGS
@given(text=any_text)
def test_tagging_never_raises(text):
    tags, reasons = features.tag_span(text)
    assert isinstance(tags, list)
    assert isinstance(reasons, (list, dict, str, type(None)))


@SETTINGS
@given(text=any_text, language=st.one_of(st.none(), st.sampled_from(
    ["en", "ms", "en-ms", "nan", "zh", "ta", "", "EN", "xx-YY", "not-a-language"]
)))
def test_the_abstention_flag_never_raises_on_any_language_tag(text, language):
    """Language tags arrive from an ASR provider, not from a validated enum.

    A provider that returns `"cmn-Hans-CN"` or `""` or nothing at all must not
    take the Glance View down.
    """
    assert isinstance(features.is_unreadable(text, language), bool)


@SETTINGS
@given(text=any_text)
def test_dosage_checking_never_raises(text):
    findings = dosage.check_text(text)
    assert isinstance(findings, list)
    for finding in findings:
        # A finding drives a blocking gate, so its fields have to be usable —
        # a NaN or an inverted range would render as a nonsense message on a
        # dialog a clinician has to act on.
        assert finding.amount_mg == finding.amount_mg  # not NaN
        assert finding.expected_low <= finding.expected_high


@SETTINGS
@given(texts=st.lists(any_text, min_size=0, max_size=4))
def test_contradiction_detection_never_raises(texts):
    entries = [_Entry(f"e{index}", text) for index, text in enumerate(texts)]
    found = contradictions.detect(entries)
    assert isinstance(found, list)
    grouped = contradictions.group(found)
    assert len(grouped) <= len(found)


@pytest.mark.parametrize(
    "content",
    [
        "",
        " ",
        "\n\n\n",
        "\t",
        "." * 500,
        "a" * 2000,
        "500mg " * 200,
        "penicillin " * 200,
        "\u0000",
        "\ufeff",
        "🙂" * 100,
        "நோயாளிக்கு கால் வீக்கம்",
        "病人的脚肿了三天",
    ],
)
def test_the_degenerate_shapes_a_generator_rarely_reaches(content):
    """Hand-picked precisely because generated text rarely produces them:
    empty, whitespace-only, one token repeated, a lone BOM, pure emoji.

    Property generation and hand-picked edge cases are complements. Neither
    finds what the other does, and using one as an argument for skipping the
    other is how a blind spot gets a rationale.
    """
    features.tag_span(content)
    features.is_unreadable(content, "en")
    dosage.check_text(content)
    contradictions.detect([_Entry("e1", content), _Entry("e2", content)])


# ==========================================================================
# Determinism — the same chart says the same thing twice
# ==========================================================================


@SETTINGS
@given(text=clinical_ish)
def test_tagging_is_deterministic(text):
    """Called on every Glance View build. A tag set that varies between two
    loads of an unchanged chart moves highlights around under the clinician's
    cursor for no visible reason."""
    assert features.tag_span(text)[0] == features.tag_span(text)[0]


@SETTINGS
@given(texts=st.lists(clinical_ish, min_size=2, max_size=4))
def test_contradiction_detection_is_deterministic(texts):
    entries = [_Entry(f"e{index}", text) for index, text in enumerate(texts)]
    first = [(c.kind, c.subject, c.left_entry_id) for c in contradictions.detect(entries)]
    second = [(c.kind, c.subject, c.left_entry_id) for c in contradictions.detect(entries)]
    assert first == second


@SETTINGS
@given(texts=st.lists(clinical_ish, min_size=2, max_size=3))
def test_a_disagreement_is_found_whichever_order_the_entries_arrive_in(texts):
    """Timeline order is a sort, and sorts change.

    Two entries disagreeing must produce the same *set* of findings whichever
    way round they are passed. Order may legitimately change which side is
    reported as `left` — the reporting rule puts the allergy first — but a
    disagreement that exists in one order and not the other means the detector
    is sensitive to something it should not be.
    """
    entries = [_Entry(f"e{index}", text) for index, text in enumerate(texts)]
    forward = {(c.kind, c.subject) for c in contradictions.detect(entries)}
    backward = {(c.kind, c.subject) for c in contradictions.detect(list(reversed(entries)))}
    assert forward == backward


@SETTINGS
@given(text=clinical_ish)
def test_dosage_findings_are_deterministic(text):
    """This one gates patient-facing writes. A gate that fires on one attempt
    and not the next teaches people to just try again — which is the failure
    mode of every intermittent check."""
    first = [(f.drug, f.stated, f.state) for f in dosage.check_text(text)]
    second = [(f.drug, f.stated, f.state) for f in dosage.check_text(text)]
    assert first == second


# ==========================================================================
# Scale — the analysers run on a whole chart, not one note
# ==========================================================================


def test_a_long_chart_does_not_blow_up_quadratically():
    """`detect` is pairwise across entries and now also within them (D-089).

    A patient with years of history is the ordinary case for a longitudinal
    record, and the Glance View has a 300ms P95 target. This is a smoke test,
    not a benchmark: it asserts the shape does not explode, which is the failure
    that would show up first as a Glance View that simply stops loading for
    exactly the long-history patients the product is for.
    """
    entries = [
        _Entry(
            f"e{index}",
            "Patient is allergic to penicillin. Started on amoxicillin 500mg TDS. "
            "Review in two weeks.",
        )
        for index in range(60)
    ]
    started = datetime.now(timezone.utc)
    found = contradictions.group(contradictions.detect(entries))
    elapsed = datetime.now(timezone.utc) - started

    assert elapsed < timedelta(seconds=5), f"detection took {elapsed}"
    # Grouping is what keeps this readable: 60 entries all recording the same
    # allergy is one clinical problem, not hundreds of pairs (D-081).
    assert len(found) <= 5, f"{len(found)} cards for one repeated disagreement"
