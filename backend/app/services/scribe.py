"""The AI scribe pipeline.

    transcript turns
        → redact_phi (per segment, stored redacted)
        → llm_client.complete (which redacts again — idempotent — and fails
          closed if anything identifying survives)
        → structured summary
        → Entry(author_role=system, type=ai_*_summary)
          + AIScribedNote(session_id, model, derived confidence, risk floor,
            redaction_count)
          + TranscriptSegment rows
        → highlight generation

Four design points worth stating, because each is a place this could have been
done more simply and less honestly.

**Redaction is applied twice, on purpose.** Segments are redacted before storage
so the database never holds an identifying transcript at rest; `llm_client`
redacts again before egress because it cannot know what its caller did. Two
independent applications of an idempotent function is the cheapest way to make
the boundary hold even if one caller is later written carelessly.

**The offline path produces a real summary, not a placeholder.** With no API key
the stub provider returns non-JSON, and `_extractive_summary` takes over: it
selects the highest-signal spans from the *already redacted* transcript using the
same feature vocabulary the Glance View scores on. The resulting note is genuine
extractive summarisation — worse than a good model, but clinically coherent, and
it means a reviewer with no key sees the real product rather than
`[STUB SUMMARY 4f3a2b1c]` sitting where a consult summary should be.
`model_used` records which path ran, so the provenance never overstates itself
(DECISIONS.md D-031).

**Confidence is derived, not asserted.** A model that self-rates gets to report
its own number. Confidence here is computed from hedging density in the source
transcript — a session where the patient said "maybe", "I think" and "not sure"
throughout produces a summary the UI marks as lower confidence. This runs on
**both** paths: a live model's self-reported number is recorded for comparison
but is not what the clinician sees, because a model's opinion of its own
reliability is not evidence about it (D-065).

**The model cannot lower a risk level, only raise it.** `_infer_risk()` computes
a deterministic floor from the transcript — explicit high-risk terms and tagged
clinical entities — and the stored `risk_level` is the *higher* of that floor
and whatever the model proposed. Model-assigned ordinals inflate and drift
between runs, and the direction that matters is the one where drift is
dangerous: a model quietly downgrading a transcript containing "chest pain" to
`low` must not be able to move the badge. Raising is allowed because a model may
legitimately notice something the keyword tables do not (D-066).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.ai import llm_client
from app.ai.redaction import redact_phi_detailed
from app.core.audit_logging import log_event
from app.core.config import settings
from app.core.enums import EntryType, InteractionType, RiskLevel, Role
from app.core.provenance import session_pointer
from app.core.sanitization import prepare_content
from app.models import AIScribedNote, AuditLog, Entry, Patient, TranscriptSegment, User, Version
from app.security import policy
from app.services import attribution, features, highlights, transcripts
from app.services.transcripts import Turn

SUMMARY_TYPE: dict[InteractionType, EntryType] = {
    InteractionType.DOCTOR_PATIENT_CONSULT: EntryType.AI_DOCTOR_CONSULT_SUMMARY,
    InteractionType.NURSE_PATIENT_CONSULT: EntryType.AI_NURSE_CONSULT_SUMMARY,
    InteractionType.AI_PATIENT_SESSION: EntryType.AI_PATIENT_SESSION_SUMMARY,
}

# Enforced at import time: if anyone ever adds a patient-facing type to the map
# above, the module fails to load rather than shipping model output to a
# patient. See policy.assert_never_patient_facing and DECISIONS.md D-067.
policy.assert_never_patient_facing(SUMMARY_TYPE.values())

SUMMARY_TITLE: dict[InteractionType, str] = {
    InteractionType.DOCTOR_PATIENT_CONSULT: "Doctor consult summary (AI-scribed)",
    InteractionType.NURSE_PATIENT_CONSULT: "Nurse consult summary (AI-scribed)",
    InteractionType.AI_PATIENT_SESSION: "Patient session summary (AI-scribed)",
}

# The model string written when the provider could not be reached and the
# deterministic summariser produced the note instead. Named rather than
# inlined because the API and the UI both need to ask "was this degraded?",
# and answering it by substring-matching a magic string in two places is how
# the two surfaces drift apart.
DEGRADED_MODEL_LABEL = "offline-extractive-v1:provider-unavailable"


def is_degraded(model_used: str | None) -> bool:
    """Was this summary produced without the model, because it was unreachable?

    Distinct from low confidence. A low-confidence summary is one the model was
    unsure about; a degraded one is a summary the model never saw. They call for
    different things from a clinician, so they are different signals and must
    not be collapsed into one badge.
    """
    return model_used == DEGRADED_MODEL_LABEL


_SYSTEM_PROMPT = (
    "You are a clinical scribe. You are reading a de-identified consult "
    "transcript; all names and identifiers have already been replaced with "
    "placeholders such as [NAME] and [PHONE]. Do not attempt to guess who "
    "anyone is. Summarise only what the transcript states — never infer a "
    "diagnosis that was not discussed. Reply with a single JSON object and no "
    "other text, using these keys: headline (one sentence), key_points (array "
    "of short strings), open_actions (array of short strings), "
    "patient_reported (array of short strings), risk_level (one of none, low, "
    "medium, high, critical), confidence (number between 0 and 1 reflecting "
    "how clearly the transcript supported this summary)."
)

# Terms that push an inferred risk level up. Deliberately conservative: the
# scribe proposes, the clinician disposes, and over-flagging a Glance View is
# how it stops being read.
_MEDIUM_RISK_TAG_PREFIXES = ("symptom:", "finding:", "entity:allergy")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Summarisation
# --------------------------------------------------------------------------


def _parse_model_json(text: str) -> dict | None:
    """Accept a bare JSON object, or one wrapped in prose or code fences."""
    if not text:
        return None
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.S)
    if fenced:
        candidate = fenced.group(1)
    elif not candidate.startswith("{"):
        braced = re.search(r"\{.*\}", candidate, re.S)
        if not braced:
            return None
        candidate = braced.group()
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extractive_summary(redacted_transcript: str, interaction_type: InteractionType) -> dict:
    """Deterministic offline summariser over the redacted transcript.

    Scores each utterance with the same feature vocabulary the Glance View uses,
    then sorts the winners into the sections a consult note actually needs.
    Same tables, same reasons — so what surfaces here and what surfaces on the
    Glance View cannot drift apart.
    """
    key_points: list[str] = []
    open_actions: list[str] = []
    patient_reported: list[str] = []
    untagged_patient: list[str] = []

    for line in redacted_transcript.splitlines():
        speaker, _, said = line.partition(":")
        said = said.strip()
        if len(said) < 12:
            continue
        tags, reasons = features.tag_span(said)
        if not reasons:
            if speaker == "patient":
                untagged_patient.append(said)
            continue

        cleaned = said.rstrip()
        if "entity:open_action" in tags and speaker in {"clinician", "staff"}:
            open_actions.append(cleaned)
        elif speaker == "patient":
            patient_reported.append(cleaned)
        else:
            key_points.append(cleaned)

    # In a pre-consult patient session the patient's own words ARE the content,
    # so an untagged worry ("my mother had problems with her feet") is exactly
    # the thing worth carrying into the room — the keyword tables have no entry
    # for family history framed as a fear. Backfilling here rather than adding
    # ever more keywords keeps the vocabulary honest about what it recognises
    # while stopping the section from coming back near-empty.
    if interaction_type is InteractionType.AI_PATIENT_SESSION:
        for said in sorted(untagged_patient, key=len, reverse=True):
            if len(patient_reported) >= 3:
                break
            if said not in patient_reported:
                patient_reported.append(said)

    # Cap each section. A summary that reproduces the transcript is not a
    # summary, and a 40-line note defeats the Glance View it feeds.
    key_points = key_points[:4]
    open_actions = open_actions[:3]
    patient_reported = patient_reported[:3]

    headline = {
        InteractionType.DOCTOR_PATIENT_CONSULT: "Doctor consult: review and plan captured.",
        InteractionType.NURSE_PATIENT_CONSULT: "Nurse consult: observations and checks captured.",
        InteractionType.AI_PATIENT_SESSION: "Pre-consult patient session: concerns captured.",
    }[interaction_type]

    # No confidence here. It is derived once, from the transcript, by
    # `derived_confidence()` on both paths — a second copy of the formula in
    # this function is exactly the drift the single-definition rule exists to
    # prevent.
    return {
        "headline": headline,
        "key_points": key_points,
        "open_actions": open_actions,
        "patient_reported": patient_reported,
        "risk_level": _infer_risk(redacted_transcript),
    }


def _infer_risk(text: str) -> str:
    """The deterministic floor. A model may raise this; it may never lower it.

    Works in canonical tag space, so it inherits every language the tagger
    knows rather than only English (D-072).
    """
    if features.high_risk_tags(text):
        return str(RiskLevel.HIGH)
    tags, _ = features.tag_span(text)
    if any(tag.startswith(prefix) for tag in tags for prefix in _MEDIUM_RISK_TAG_PREFIXES):
        return str(RiskLevel.MEDIUM)
    return str(RiskLevel.LOW)


def _render_content(summary: dict) -> str:
    """Plain text. Not Markdown, not HTML — the render path escapes text and the
    storage rule says content is plain (D-015)."""
    lines: list[str] = [str(summary.get("headline") or "Consult summary.").strip()]

    def section(title: str, items: list) -> None:
        rows = [str(item).strip() for item in items or [] if str(item).strip()]
        if not rows:
            return
        lines.append("")
        lines.append(title)
        lines.extend(f"- {row}" for row in rows)

    section("Key points", summary.get("key_points") or [])
    section("Open actions", summary.get("open_actions") or [])
    section("Reported by the patient", summary.get("patient_reported") or [])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def _gazetteer(db: Session, patient: Patient) -> set[str]:
    """Known synthetic names in scope, so bare first-name mentions are caught
    alongside the pattern-based detections."""
    names = {patient.name}
    for user in db.query(User).filter(User.clinic_id == patient.clinic_id).all():
        names.add(user.name)
    return names


class RegenerationRefused(RuntimeError):
    """Regeneration would have destroyed something a human did.

    The capability being protected is "AI regeneration that preserves
    human-confirmed and completed state". The cheap reading of that is "keep
    the accepted highlights", and highlights already survive because
    regeneration reuses the entry id (D-059). The expensive reading, and the
    one that matters, is that **a clinician's own words must never be replaced
    by a model's second attempt.**

    So regeneration refuses rather than merging when a human has edited the
    summary. Merging would require deciding which sentences of a clinician's
    edit to keep, and that is a clinical judgement the system has no standing
    to make — the same rule that stops the contradiction detector picking a
    winner between two humans (D-068).

    Refusing is recoverable: the clinician can revert to the machine version
    and regenerate, or copy their edit out first. Overwriting is not.
    """

    def __init__(self, message: str, *, session_id: str, reason: str) -> None:
        super().__init__(message)
        self.session_id = session_id
        self.reason = reason


def _assert_no_human_edits(db: Session, entry: Entry) -> None:
    """Refuse if any version of this entry was written by someone other than the model."""
    human = (
        db.query(Version)
        .filter(Version.entry_id == entry.id, Version.edited_by_role != Role.SYSTEM)
        .order_by(Version.version_number.desc())
        .first()
    )
    if human is not None:
        raise RegenerationRefused(
            "A clinician has edited this summary. Regenerating would discard "
            "their wording. Revert to the machine version first, or copy the "
            "edit out before regenerating.",
            session_id="",
            reason="human_edited",
        )


def run_scribe(
    db: Session,
    *,
    patient: Patient,
    interaction_type: InteractionType,
    turns: list[Turn] | None = None,
    actor_id: str = "system",
    session_id: str | None = None,
    regenerate: bool = False,
) -> Entry:
    """Run one transcript through the pipeline and append the result.

    Synchronous by design for a 72-hour build: a background worker would need
    its own session, its own failure surface and its own retry story, none of
    which the demo exercises. The visible "processing" state is rendered by the
    client for the duration of this call (DECISIONS.md D-032).
    """
    if settings.scribe_delay_ms:
        # Demo affordance only: makes the client's processing state observable.
        # Zero in tests and by default.
        time.sleep(settings.scribe_delay_ms / 1000.0)

    turns = turns or transcripts.build_turns(
        interaction_type,
        patient_name=patient.name,
        patient_mrn=patient.mrn,
        patient_id=patient.id,
    )
    session_id = session_id or f"sess-{patient.id}-{uuid.uuid4().hex[:8]}"
    gazetteer = _gazetteer(db, patient)

    # --- regeneration ----------------------------------------------------
    # Re-running a session used to be undefined behaviour: a fresh session id
    # produced a duplicate summary entry, and passing the same one crashed on
    # the transcript_segments unique constraint. Neither is an answer to "the
    # model produced a poor summary, run it again" (D-078).
    existing_note = (
        db.query(AIScribedNote).filter(AIScribedNote.session_id == session_id).one_or_none()
    )
    existing_entry: Entry | None = None
    if existing_note is not None:
        if not regenerate:
            raise RegenerationRefused(
                "This session already has a summary. Pass regenerate=True to replace it.",
                session_id=session_id,
                reason="exists",
            )
        existing_entry = db.query(Entry).filter(Entry.id == existing_note.entry_id).one()
        _assert_no_human_edits(db, existing_entry)

    # --- redact, then store segments already redacted --------------------
    redaction_count = 0
    redacted_lines: list[str] = []
    # Turns the vocabulary could not read. Counted on the redacted text, since
    # that is what every downstream feature actually sees.
    unreadable_count = 0
    for index, turn in enumerate(turns):
        result = redact_phi_detailed(turn.text, gazetteer=gazetteer)
        redaction_count += result.replacements
        redacted_lines.append(f"{turn.speaker}: {result.text}")
        if features.is_unreadable(result.text, turn.language):
            unreadable_count += 1
        if existing_entry is not None:
            # Segments are immutable and already stored under this session id.
            # The transcript is the source of truth; regeneration re-reads it,
            # it does not re-record it.
            continue
        db.add(
            TranscriptSegment(
                session_id=session_id,
                clinic_id=patient.clinic_id,
                sequence=index,
                speaker_label=turn.speaker,
                start_ms=turn.start_ms,
                end_ms=turn.end_ms,
                redacted_text=result.text,
                confidence=turn.confidence,
                language=turn.language,
            )
        )
    redacted_transcript = "\n".join(redacted_lines)

    # --- summarise -------------------------------------------------------
    # The chokepoint. Redaction runs inside complete() as well; if anything
    # identifying survived, it raises rather than sending.
    try:
        response = llm_client.complete(
            redacted_transcript,
            system=_SYSTEM_PROMPT,
            gazetteer=gazetteer,
            purpose=f"ai_scribe:{interaction_type}",
            actor_id=actor_id,
            clinic_id=patient.clinic_id,
        )
    except llm_client.LLMUnavailableError:
        # The provider is down or too slow. The deterministic summariser below
        # is the same one used when no model is configured at all, so there is
        # a real summary either way — the clinician loses fluency, not the
        # consult. The label says the model was unreachable, so a degraded note
        # is legible as degraded rather than merely worse. The specific reason
        # is in the audit log, recorded at the chokepoint. See DECISIONS.md D-070.
        response = None

    if response is None:
        summary = _extractive_summary(redacted_transcript, interaction_type)
        model_used = DEGRADED_MODEL_LABEL
        model_self_reported = None
    else:
        parsed = _parse_model_json(response.text)
        if parsed and parsed.get("headline"):
            summary = parsed
            model_used = f"{response.provider}:{response.model}"
            model_self_reported = _clamp_confidence(parsed.get("confidence"), None, default=None)
        else:
            # No live model, or the model did not return usable JSON. Same code
            # path, deterministic summariser, and the model field says so.
            summary = _extractive_summary(redacted_transcript, interaction_type)
            model_used = "offline-extractive-v1"
            model_self_reported = None

    # Confidence is measured from the source, on both paths. A live model's own
    # number is kept for comparison but is never what the clinician sees.
    confidence = derived_confidence(redacted_transcript)

    content, markers = prepare_content(_render_content(summary))

    # The model proposes a risk level; deterministic rules set the floor it
    # cannot go below. `risk_floor_applied` records when the two disagreed, so
    # "the badge says high because a rule said so, not because a model felt
    # strongly" is answerable from the row rather than from this comment.
    model_risk = _coerce_risk(summary.get("risk_level"))
    floor = RiskLevel(_infer_risk(redacted_transcript))
    risk_level = _max_risk(model_risk, floor)
    risk_floor_applied = RISK_RANK[floor] > RISK_RANK[model_risk]

    # --- persist ---------------------------------------------------------
    if existing_entry is not None:
        # Regeneration. Reuse the entry, append a version. The entry id is what
        # every accepted highlight, comment, task and provenance pointer is
        # anchored to, so keeping it is what makes "preserves human-confirmed
        # state" true rather than aspirational. Highlights anchored to the old
        # version go stale and render side by side (D-076), which is exactly
        # the right outcome: a clinician sees what they confirmed and what the
        # model now says, and decides.
        entry = existing_entry
        next_version = entry.version_number + 1
        entry.content = content
        entry.risk_level = risk_level
        entry.version_number = next_version

        version = Version(
            entry_id=entry.id,
            version_number=next_version,
            content_snapshot=content,
            title_snapshot=entry.title,
            risk_level_snapshot=str(risk_level),
            edited_by="system",
            edited_by_role=Role.SYSTEM,
            change_summary=f"ai scribe regenerated ({model_used})",
        )
        db.add(version)
        db.flush()
        entry.current_version_id = version.id

        existing_note.model_used = model_used
        existing_note.confidence = confidence
        existing_note.model_self_reported_confidence = model_self_reported
        existing_note.risk_floor_applied = risk_floor_applied
        existing_note.unreadable_segment_count = unreadable_count
        existing_note.model_proposed_risk = str(model_risk)
        existing_note.redaction_count = redaction_count

        db.add(
            AuditLog(
                actor_id=actor_id,
                actor_role=Role.SYSTEM,
                clinic_id=patient.clinic_id,
                action="entry.ai_scribe_regenerated",
                target_type="entry",
                target_id=entry.id,
                audit_metadata=json.dumps(
                    {"session_id": session_id, "version": next_version, "model": model_used}
                ),
            )
        )
        highlights.refresh_entry_highlights(db, entry)
        db.commit()
        db.refresh(entry)
        return entry

    entry = Entry(
        patient_id=patient.id,
        clinic_id=patient.clinic_id,
        author_role=Role.SYSTEM,
        author_id="system",
        type=SUMMARY_TYPE[interaction_type],
        title=SUMMARY_TITLE[interaction_type],
        content=content,
        risk_level=risk_level,
        version_number=1,
        # Points at the originating session, not at itself: this note is
        # derived text and the source of truth is the transcript behind it.
        provenance_pointer=session_pointer(session_id),
    )
    db.add(entry)
    db.flush()

    version = Version(
        entry_id=entry.id,
        version_number=1,
        content_snapshot=content,
        title_snapshot=entry.title,
        risk_level_snapshot=str(risk_level),
        edited_by="system",
        edited_by_role=Role.SYSTEM,
        change_summary="ai scribe generated",
    )
    db.add(version)
    db.flush()
    entry.current_version_id = version.id

    db.add(
        AIScribedNote(
            entry_id=entry.id,
            clinic_id=patient.clinic_id,
            session_id=session_id,
            interaction_type=str(interaction_type),
            model_used=model_used,
            redaction_applied=True,
            redaction_count=redaction_count,
            confidence=confidence,
            model_self_reported_confidence=model_self_reported,
            risk_floor_applied=risk_floor_applied,
            unreadable_segment_count=unreadable_count,
            model_proposed_risk=str(model_risk),
        )
    )
    db.add(
        AuditLog(
            actor_id=actor_id,
            actor_role=Role.SYSTEM,
            clinic_id=patient.clinic_id,
            action="entry.ai_scribe",
            target_type="entry",
            target_id=entry.id,
            audit_metadata=json.dumps(
                {
                    "type": str(entry.type),
                    "session_id": session_id,
                    "model": model_used,
                    "redactions": redaction_count,
                    "confidence": confidence,
                    "injection_markers": markers,
                }
            ),
        )
    )
    db.flush()

    highlights.refresh_entry_highlights(db, entry)

    # Line-level provenance, for every AI-scribed note rather than only for
    # voice captures. The segments are already here and the matching is the
    # same work, so the Phase 2 fixture path gets "which spoken line produced
    # this bullet" for free (Phase 5; see services/attribution.py).
    links = attribution.link_summary_to_segments(db, entry=entry, session_id=session_id)

    log_event(
        actor_id=actor_id,
        action="entry.ai_scribe",
        target_type="entry",
        target_id=entry.id,
        clinic_id=patient.clinic_id,
        metadata={
            "interaction_type": str(interaction_type),
            "model": model_used,
            "redactions": redaction_count,
            "segments": len(turns),
            "confidence": confidence,
            "confidence_band": confidence_band(confidence),
            "risk_level": str(risk_level),
            "risk_floor_applied": risk_floor_applied,
            "attributed_lines": len(links),
        },
    )
    db.commit()
    db.refresh(entry)
    return entry


def _clamp_confidence(value, fallback: float | None, *, default: float | None = 0.5):
    try:
        number = float(value)
    except (TypeError, ValueError):
        if fallback is None:
            return default
        number = float(fallback)
    return round(max(0.0, min(1.0, number)), 2)


# Confidence bands. These exist so that "medium" has a number behind it and the
# number has a meaning behind it, rather than being a word the UI picked.
#
#   high    >= 0.75   little hedging in the source; the summary restates
#                     things the transcript said plainly
#   medium  0.60-0.75 some hedging; worth a glance at the source
#   low     <  0.60   the source was substantially uncertain — the UI flags
#                     this and tells the reader to verify against the source
#
# LOW_BAND is the same 0.60 the Glance View flags on (glance.LOW_CONFIDENCE_
# THRESHOLD); the duplication is asserted equal by test rather than imported,
# because the two modules should be free to disagree loudly rather than quietly.
CONFIDENCE_HIGH_BAND = 0.75
CONFIDENCE_LOW_BAND = 0.60

# Bounds on the derived figure. Never 1.0: a summariser working from a
# transcript it did not hear, through a recogniser that may have erred, has no
# business claiming certainty. Never 0.0 either — a floor of 0.35 keeps the
# number a comparison rather than a verdict.
CONFIDENCE_CEILING = 0.90
CONFIDENCE_FLOOR = 0.35


def derived_confidence(redacted_transcript: str) -> float:
    """Confidence measured from the source text, not reported by the model.

    Hedging density is a weak proxy — it measures how certain the *speakers*
    were, which correlates with but is not the same as how well the summary is
    supported. It has one property self-reported confidence does not: it is
    computed from something a reviewer can go and read. A number that can be
    checked against the transcript is worth more than a better-calibrated one
    that cannot (D-065).
    """
    hedging = features.uncertainty_ratio(redacted_transcript)
    value = CONFIDENCE_CEILING - 0.02 - 0.9 * hedging
    return round(max(CONFIDENCE_FLOOR, min(CONFIDENCE_CEILING, value)), 2)


def confidence_band(value: float | None) -> str:
    """The word for a number. One definition, so the UI cannot invent another."""
    if value is None:
        return "unknown"
    if value >= CONFIDENCE_HIGH_BAND:
        return "high"
    if value >= CONFIDENCE_LOW_BAND:
        return "medium"
    return "low"


RISK_RANK: dict[RiskLevel, int] = {
    RiskLevel.NONE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


def _max_risk(*levels: RiskLevel) -> RiskLevel:
    return max(levels, key=lambda level: RISK_RANK[level])


def _coerce_risk(value) -> RiskLevel:
    try:
        return RiskLevel(str(value))
    except ValueError:
        return RiskLevel.LOW
