"""Read your logs.

The feedback's third item: *"Your redaction works. Now read your logs. Grep
your application logs, error traces, crash reports and any third-party
dashboard for an IC number, a phone number, a patient name. Redaction before
the model is the door everyone guards. What is going out the other ones?"*

The existing suite guards the model door well — `test_llm_chokepoint.py` proves
nothing reaches an LLM un-redacted, and `test_url_surface.py` closed a phone
number that was travelling in a query string. Both were found by looking at a
door someone had thought about.

This file does not pick a door. It writes content containing every kind of
identifier the redactor knows about, drives the operations that touch it, and
greps **everything the process emitted** — every log record from every logger,
at every level, including the exception paths — for any fragment of it.

Why generated content
---------------------
A hand-written test asserts "the note body is not in the log". It passes, and
says nothing about the *title*, the *change summary*, the *comment*, the
*mention*, or the audit metadata dict that happens to carry a snippet for
convenience. Generating the identifier and searching for it by value catches
the leak wherever it comes out, including from a field nobody listed.

What counts as a leak
---------------------
Any occurrence of the generated identifier or the free-text body in any log
record. IDs, action names, types, counts and timestamps are all fine — those
are what the logs are *for*, and a log with no identifiers is a log nobody can
use to investigate an incident.
"""

from __future__ import annotations

import logging

import pytest

from app.core.enums import Role

# Distinctive enough that a substring hit is a real leak and not a coincidence.
# Synthetic, obviously: this repo never touches real PHI.
CANARY_NRIC = "S7654321Z"
CANARY_PHONE = "+65 9876 5432"
CANARY_EMAIL = "zzz.canary@example.sg"
CANARY_NAME = "Zephaniah Quillsworth"
CANARY_BODY = (
    f"Spoke to {CANARY_NAME} about the swelling. IC {CANARY_NRIC}, "
    f"contact {CANARY_PHONE} or {CANARY_EMAIL}. "
    "Discussed titration and agreed to review."
)

CANARIES = [CANARY_NRIC, CANARY_PHONE, CANARY_EMAIL, CANARY_NAME]

# Substrings of the free text itself. The identifiers above could plausibly be
# redacted on their way to a log; the prose would not be, so it is the better
# probe for "the whole body got logged".
BODY_FRAGMENTS = ["swelling", "titration", "agreed to review"]


def _all_log_text(caplog) -> str:
    """Everything the process emitted, formatted the way a log file would be.

    `record.message` alone misses `%s`-style lazy formatting and misses the
    exception text entirely — which is exactly where a crash path puts the
    request body. This renders each record fully, arguments and traceback
    included, because a leak in a stack trace is still a leak.
    """
    parts = []
    for record in caplog.records:
        try:
            parts.append(record.getMessage())
        except Exception:  # a broken format string is not what we are testing
            parts.append(str(record.msg))
        if record.exc_info:
            import traceback

            parts.append("".join(traceback.format_exception(*record.exc_info)))
        for value in vars(record).values():
            if isinstance(value, (str, dict, list, tuple)):
                parts.append(str(value))
    return "\n".join(parts)


def _assert_clean(caplog, *, context: str):
    text = _all_log_text(caplog)
    for canary in CANARIES:
        assert canary not in text, (
            f"{context}: the identifier {canary!r} reached a log. "
            "Redaction before the model is not the only door."
        )
    for fragment in BODY_FRAGMENTS:
        assert fragment not in text, (
            f"{context}: the note body ({fragment!r}) reached a log. Logs "
            "carry ids, action types and timestamps — never content."
        )


@pytest.fixture()
def capture_everything(caplog):
    """Every logger, every level. A leak that only appears at DEBUG is still a
    leak — debug logging gets switched on in production precisely when someone
    is investigating, which is the worst moment to start emitting PHI."""
    caplog.set_level(logging.DEBUG)
    return caplog


def test_creating_an_entry_logs_no_content(client_p1, token_for, capture_everything):
    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    response = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "staff_note", "title": CANARY_NAME, "content": CANARY_BODY},
    )
    assert response.status_code == 201, response.text
    _assert_clean(capture_everything, context="entry.create")


def test_editing_an_entry_logs_no_content(client_p1, token_for, capture_everything):
    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    created = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "staff_note", "content": "placeholder"},
    )
    capture_everything.clear()

    response = client_p1.patch(
        f"/entries/{created.json()['id']}",
        headers=headers,
        json={
            "content": CANARY_BODY,
            "expected_version": created.json()["version_number"],
            "change_summary": f"per {CANARY_NAME}",
        },
    )
    assert response.status_code == 200, response.text
    _assert_clean(capture_everything, context="entry.update")


def test_comments_and_mentions_log_no_content(client_p1, token_for, capture_everything):
    """Comment bodies are the field most easily forgotten.

    They are not Entry content, so a hand-written assertion about "the note"
    does not cover them, and they carry exactly the same free text.
    """
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    created = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "clinician_section", "content": "placeholder"},
    )
    capture_everything.clear()

    response = client_p1.post(
        f"/entries/{created.json()['id']}/comments",
        headers=headers,
        json={"body": CANARY_BODY},
    )
    assert response.status_code in (200, 201), response.text
    _assert_clean(capture_everything, context="comment.create")


def test_a_rejected_write_logs_no_content(client_p1, token_for, capture_everything):
    """The refusal path, which is where a body most often gets logged.

    "Log the payload so we can see why it failed" is the single most natural
    thing to add to an error handler, and it is how a validation failure turns
    into a PHI disclosure with a long retention period.
    """
    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    response = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        # staff cannot author a clinician_section — a server-side refusal
        json={"type": "clinician_section", "content": CANARY_BODY},
    )
    assert response.status_code == 403, response.text
    _assert_clean(capture_everything, context="rejected write")


def test_a_validation_failure_logs_no_content(client_p1, token_for, capture_everything):
    """FastAPI's 422 handler echoes the offending input by default. That is a
    good developer experience and a bad PHI posture, and the two are only
    distinguishable by looking."""
    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    response = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "not_a_real_type", "content": CANARY_BODY},
    )
    assert response.status_code == 422
    _assert_clean(capture_everything, context="validation failure")


def test_a_cross_clinic_refusal_logs_no_content(client_p1, token_for, capture_everything):
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    response = client_p1.post(
        "/patients/patient-b1/entries",
        headers=headers,
        json={"type": "clinician_section", "content": CANARY_BODY},
    )
    assert response.status_code in (403, 404)
    _assert_clean(capture_everything, context="cross-clinic refusal")


def test_the_scribe_pipeline_logs_no_transcript(client_p1, token_for, capture_everything):
    """The longest free-text object in the system, and the one that passes
    through the most stages — transcript, redaction, model call, summary."""
    headers = token_for("u-a-clinician", Role.CLINICIAN, "clinic-a")
    response = client_p1.post(
        "/patients/patient-a1/scribe",
        headers=headers,
        json={"interaction_type": "doctor_patient_consult"},
    )
    assert response.status_code in (200, 201, 400, 422), response.text
    _assert_clean(capture_everything, context="scribe pipeline")


def test_the_canary_would_actually_be_detected(client_p1, token_for, capture_everything):
    """The guard on the guard.

    Every assertion above is a search for a string. If `_all_log_text` returned
    "" — a caplog fixture misconfigured, a logger not propagating — they would
    all pass while reading nothing at all. This deliberately logs a canary and
    asserts the detector sees it.
    """
    logging.getLogger("carenote.test").warning("probe %s", CANARY_NRIC)
    assert CANARY_NRIC in _all_log_text(capture_everything), (
        "the log detector cannot see log output, so every other assertion in "
        "this file is passing vacuously"
    )


def test_logs_still_carry_what_an_investigation_needs(
    client_p1, token_for, capture_everything
):
    """The counterweight.

    A build that logged nothing at all would pass every test above and be
    useless the first time someone had to answer "who changed this note, and
    when". Hygiene means logging ids and actions rather than content — not
    logging less.
    """
    headers = token_for("u-a-staff", Role.STAFF, "clinic-a")
    created = client_p1.post(
        "/patients/patient-a1/entries",
        headers=headers,
        json={"type": "staff_note", "content": CANARY_BODY},
    )
    entry_id = created.json()["id"]
    text = _all_log_text(capture_everything)

    assert entry_id in text, "the entry id is not in the audit trail"
    assert "u-a-staff" in text, "the actor is not in the audit trail"
    assert "entry.create" in text, "the action is not in the audit trail"
