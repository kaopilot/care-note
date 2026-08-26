"""Required test 3 of 4 — every highlight resolves to a real source.

Named by the brief. This is the trust deliverable: the Glance View makes claims
about what matters, and a claim a clinician cannot trace is a claim they have to
take on faith. The brief's requirement is that clicking a highlight lands on its
originating entry *and span* in the timeline, for AI-scribed and manual content
alike.

So the assertion is not "the pointer is a non-empty string". It is that the
pointer **resolves**, through the same `resolve()` code path the application
uses, to a real span of real text — and that the text it resolves to is the text
the card showed. A pointer that resolves to the wrong words is worse than no
pointer, because it looks like evidence.

`resolve()` raises on a dangling or out-of-range pointer rather than returning
empty (D-008), which is what makes "every pointer resolves" a testable property
rather than a hopeful one.
"""

from __future__ import annotations

import pytest

from app.core.enums import Role
from app.core.provenance import ProvenanceError, parse, resolve
from app.models import Entry, Highlight

# Long enough that the scorer finds several taggable spans, and seeded with
# clinical vocabulary the feature extractor recognises.
RISKY_NOTE = (
    "Patient reports missing the evening metformin dose most weeknights. "
    "Allergy to penicillin confirmed with the patient today. "
    "BP 156/94 on recheck, higher than the last two visits."
)


@pytest.fixture()
def clinician(token_for):
    return token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")


@pytest.fixture()
def staff(token_for):
    return token_for("u-a-staff", Role.STAFF, "clinic-a")


@pytest.fixture()
def ai_entry(client_p1, clinician):
    """A real AI-scribed note: transcript -> redaction -> LLM -> stored Entry."""
    response = client_p1.post(
        "/patients/patient-a1/scribe",
        json={"interaction_type": "doctor_patient_consult"},
        headers=clinician,
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture()
def populated(client_p1, clinician, staff, ai_entry):
    """A chart with highlights from both machine and human, AI and manual."""
    client_p1.post(
        "/patients/patient-a1/entries",
        json={"type": "staff_note", "content": RISKY_NOTE},
        headers=staff,
    )
    return ai_entry


# --------------------------------------------------------------------------
# Highlights exist, including from AI-scribed notes
# --------------------------------------------------------------------------


def test_highlights_are_generated_across_the_chart(client_p1, clinician, populated):
    highlights = client_p1.get(
        "/patients/patient-a1/highlights", headers=clinician
    ).json()
    assert highlights, "a populated chart surfaced no highlights at all"


def test_at_least_one_highlight_is_sourced_from_an_ai_scribed_note(
    client_p1, clinician, populated
):
    """The brief asks specifically for this case — AI-sourced highlights are
    the ones whose traceability the clinician most needs."""
    highlights = client_p1.get(
        "/patients/patient-a1/highlights", headers=clinician
    ).json()
    ai_sourced = [h for h in highlights if h["is_ai_scribed"]]
    assert ai_sourced, "no highlight came from an AI-scribed note"


def test_an_ai_sourced_highlight_says_so_in_its_reason(
    client_p1, clinician, populated
):
    """A clinician reading the card should not have to click through to
    discover that the thing being asserted came from a machine."""
    highlights = client_p1.get(
        "/patients/patient-a1/highlights", headers=clinician
    ).json()
    ai_sourced = [h for h in highlights if h["is_ai_scribed"]]
    assert all("AI-scribed note" in h["risk_reason"] for h in ai_sourced)


# --------------------------------------------------------------------------
# Every pointer resolves — the core requirement
# --------------------------------------------------------------------------


def test_every_highlight_has_a_reason_and_a_pointer(client_p1, clinician, populated):
    """The brief's hard constraint on the Glance View, restated as a schema
    check: nothing is surfaced without both."""
    highlights = client_p1.get(
        "/patients/patient-a1/highlights", headers=clinician
    ).json()
    for highlight in highlights:
        assert highlight["risk_reason"].strip(), highlight["id"]
        assert highlight["provenance_pointer"].strip(), highlight["id"]


def test_every_pointer_resolves_to_a_real_entry_and_span(
    client_p1, clinician, populated, seeded_p1
):
    """The headline assertion, run through the application's own resolver.

    Resolution is exercised at the service layer here so that a highlight on
    *any* entry type is covered, including those a given role would be refused
    at the API. The API-level equivalent is asserted separately below.
    """
    db = seeded_p1["db"]
    highlights = db.query(Highlight).filter(Highlight.patient_id == "patient-a1").all()
    assert highlights, "nothing to assert against"

    for highlight in highlights:
        resolved = resolve(db, highlight.provenance_pointer, clinic_id="clinic-a")
        assert resolved["kind"] == "entry"
        assert resolved["entry_id"] == highlight.entry_id
        assert resolved["span"] == (highlight.span_start, highlight.span_end)
        assert db.get(Entry, resolved["entry_id"]) is not None


def test_every_pointer_carries_a_span_not_merely_an_entry(
    client_p1, clinician, populated, seeded_p1
):
    """"Jump to the source" means the words, not the note.

    An entry-level pointer would drop a clinician at the top of a long summary
    and leave them to find the phrase themselves, which is the scrolling-and-
    guessing the product exists to remove.
    """
    db = seeded_p1["db"]
    for highlight in db.query(Highlight).all():
        parsed = parse(highlight.provenance_pointer)
        assert parsed.fragment_kind == "span", highlight.provenance_pointer
        assert parsed.span_end > parsed.span_start


def test_the_resolved_text_is_the_text_the_card_displayed(
    client_p1, clinician, populated
):
    """A pointer that resolves to different words than the card showed would be
    worse than no pointer — it looks like corroboration while contradicting the
    claim it is attached to."""
    highlights = client_p1.get(
        "/patients/patient-a1/highlights", headers=clinician
    ).json()

    for highlight in highlights:
        resolved = client_p1.get(
            "/provenance",
            params={"pointer": highlight["provenance_pointer"]},
            headers=clinician,
        )
        assert resolved.status_code == 200, highlight["provenance_pointer"]
        assert resolved.json()["span_text"] == highlight["span_text"]


def test_the_resolved_span_is_really_inside_the_source_entry_content(
    client_p1, clinician, populated, seeded_p1
):
    """Closing the loop against stored content rather than against the API's
    own idea of it: the offsets must index the real note."""
    db = seeded_p1["db"]
    for highlight in db.query(Highlight).all():
        entry = db.get(Entry, highlight.entry_id)
        assert entry is not None
        assert highlight.span_end <= len(entry.content)
        # The highlight is anchored to a version; on an unedited entry the
        # anchored text and the current text are the same words.
        if highlight.source_version_number == entry.version_number:
            assert entry.content[highlight.span_start : highlight.span_end] == (
                highlight.span_text
            )


def test_glance_view_highlights_resolve_too(client_p1, clinician, populated):
    """The Glance View is the surface the clinician actually clicks, and it
    assembles its own payload — so its pointers are asserted separately from
    the highlight list's."""
    glance = client_p1.get("/patients/patient-a1/glance", headers=clinician).json()
    assert glance["highlights"], "the Top Card surfaced nothing"

    for highlight in glance["highlights"]:
        resolved = client_p1.get(
            "/provenance",
            params={"pointer": highlight["provenance_pointer"]},
            headers=clinician,
        )
        assert resolved.status_code == 200
        assert resolved.json()["entry_id"] == highlight["entry_id"]


# --------------------------------------------------------------------------
# Manual highlights, including inside AI-scribed notes
# --------------------------------------------------------------------------


def test_a_manual_highlight_inside_an_ai_note_resolves_to_that_note(
    client_p1, clinician, ai_entry
):
    """Scenario B from the brief: a clinician marks a phrase inside a machine
    summary. The annotation must point back into the AI note itself."""
    created = client_p1.post(
        f"/entries/{ai_entry['id']}/highlights",
        json={"span_start": 0, "span_end": 40},
        headers=clinician,
    )
    assert created.status_code == 201
    highlight = created.json()
    assert highlight["is_manual"] is True

    resolved = client_p1.get(
        "/provenance",
        params={"pointer": highlight["provenance_pointer"]},
        headers=clinician,
    ).json()
    assert resolved["entry_id"] == ai_entry["id"]
    assert resolved["span_text"] == highlight["span_text"]


def test_an_ai_entry_points_back_to_the_session_that_produced_it(
    client_p1, clinician, ai_entry
):
    """Two levels of provenance, and the second is the one that matters for
    trust: the highlight points into the summary, and the summary points at the
    transcript session it was derived from — never at itself."""
    assert ai_entry["provenance_pointer"].startswith("session://")

    resolved = client_p1.get(
        "/provenance",
        params={"pointer": ai_entry["provenance_pointer"]},
        headers=clinician,
    ).json()
    assert resolved["kind"] == "session"
    assert resolved["entry_id"] == ai_entry["id"]
    assert resolved["session_id"] == ai_entry["ai_session_id"]


def test_the_transcript_segments_behind_a_session_resolve_individually(
    client_p1, clinician, ai_entry
):
    """The bottom of the provenance chain: an actual diarised segment, stored
    already-redacted."""
    resolved = client_p1.get(
        "/provenance",
        params={"pointer": f"transcript://{ai_entry['ai_session_id']}#segment:0"},
        headers=clinician,
    )
    assert resolved.status_code == 200
    assert resolved.json()["kind"] == "transcript_segment"
    assert resolved.json()["span_text"], "a segment resolved to no text"


# --------------------------------------------------------------------------
# A pointer is a reference, never an authorisation
# --------------------------------------------------------------------------


def test_a_dangling_pointer_raises_rather_than_resolving_empty(seeded_p1):
    """This is what makes "every pointer resolves" meaningful.

    If `resolve()` returned `{}` for a missing target, every assertion in this
    file would pass against a database with no rows in it.
    """
    with pytest.raises(ProvenanceError):
        resolve(seeded_p1["db"], "entry://no-such-entry", clinic_id="clinic-a")


def test_a_span_beyond_the_end_of_the_entry_raises(seeded_p1):
    """Out-of-range offsets must fail loudly rather than silently clamping to a
    shorter string — a truncated quote is a misquote."""
    with pytest.raises(ProvenanceError):
        resolve(
            seeded_p1["db"], "entry://entry-a1-clin#span:0-99999", clinic_id="clinic-a"
        )


def test_resolution_obeys_the_same_role_rules_as_reading(
    client_p1, clinician, staff
):
    """Staff cannot read clinician sections, so a pointer into one must not
    become the side door. The highlight is created by a clinician — who is
    allowed — and then resolution is attempted as staff."""
    highlight = client_p1.post(
        "/entries/entry-a1-clin/highlights",
        json={"span_start": 0, "span_end": 20},
        headers=clinician,
    )
    assert highlight.status_code == 201

    refused = client_p1.get(
        "/provenance",
        params={"pointer": highlight.json()["provenance_pointer"]},
        headers=staff,
    )
    assert refused.status_code == 403
    assert "HbA1c" not in refused.text


def test_a_pointer_does_not_resolve_across_a_clinic_boundary(seeded_p1):
    """Enforced inside `resolve()` itself, so no caller can forget it."""
    with pytest.raises(ProvenanceError):
        resolve(seeded_p1["db"], "entry://entry-a1-clin", clinic_id="clinic-b")


# --------------------------------------------------------------------------
# Anchoring: a highlight points at the words it was made against
# --------------------------------------------------------------------------


def test_an_edited_entry_leaves_its_highlight_stale_but_still_resolvable(
    client_p1, clinician
):
    """D-030. After an edit the highlight is marked stale rather than silently
    re-anchored, and it still resolves — to the words as they read when the
    clinician marked them, taken from the version snapshot.

    Silently sliding the span onto the current text would show a clinician's
    confirmed highlight sitting over words nobody approved.
    """
    created = client_p1.post(
        "/entries/entry-a1-clin/highlights",
        json={"span_start": 0, "span_end": 20},
        headers=clinician,
    ).json()
    assert created["stale"] is False
    original_text = created["span_text"]

    client_p1.patch(
        "/entries/entry-a1-clin",
        json={
            "content": "Rewritten assessment with entirely different wording now.",
            "expected_version": 1,
        },
        headers=clinician,
    )

    after = client_p1.get("/patients/patient-a1/highlights", headers=clinician).json()
    stale = next(h for h in after if h["id"] == created["id"])
    assert stale["stale"] is True
    assert stale["span_text"] == original_text, "the highlight silently re-anchored"
    assert stale["provenance_pointer"] == created["provenance_pointer"]
