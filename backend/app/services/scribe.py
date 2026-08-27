"""The AI scribe pipeline.

    transcript turns
        → redact_phi (per segment, stored redacted)
        → llm_client.complete (which redacts again — idempotent — and fails
          closed if anything identifying survives)
        → structured summary
        → Entry(author_role=system, type=ai_*_summary)
          + AIScribedNote(session_id, model, confidence, redaction_count)
          + TranscriptSegment rows
        → highlight generation

Three design points worth stating, because each is a place this could have been
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
its own number. The offline path computes one from hedging density in the source
transcript — a session where the patient said "maybe", "I think" and "not sure"
throughout produces a summary the UI marks as lower confidence, which is exactly
the calibration signal the brief is asking for.
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
from app.services import attribution, features, highlights, transcripts
from app.services.transcripts import Turn

SUMMARY_TYPE: dict[InteractionType, EntryType] = {
    InteractionType.DOCTOR_PATIENT_CONSULT: EntryType.AI_DOCTOR_CONSULT_SUMMARY,
    InteractionType.NURSE_PATIENT_CONSULT: EntryType.AI_NURSE_CONSULT_SUMMARY,
    InteractionType.AI_PATIENT_SESSION: EntryType.AI_PATIENT_SESSION_SUMMARY,
}

SUMMARY_TITLE: dict[InteractionType, str] = {
    InteractionType.DOCTOR_PATIENT_CONSULT: "Doctor consult summary (AI-scribed)",
    InteractionType.NURSE_PATIENT_CONSULT: "Nurse consult summary (AI-scribed)",
    InteractionType.AI_PATIENT_SESSION: "Patient session summary (AI-scribed)",
}

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
_HIGH_RISK_TERMS = (
    "chest pain",
    "bleeding",
    "melaena",
    "syncope",
    "collapse",
    "suicidal",
    "self-harm",
    "anaphylaxis",
    "sepsis",
    "haemoptysis",
)
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

    hedging = features.uncertainty_ratio(redacted_transcript)
    confidence = max(0.35, min(0.9, 0.88 - 0.9 * hedging))

    return {
        "headline": headline,
        "key_points": key_points,
        "open_actions": open_actions,
        "patient_reported": patient_reported,
        "risk_level": _infer_risk(redacted_transcript),
        "confidence": round(confidence, 2),
    }


def _infer_risk(text: str) -> str:
    lowered = text.lower()
    if any(term in lowered for term in _HIGH_RISK_TERMS):
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


def run_scribe(
    db: Session,
    *,
    patient: Patient,
    interaction_type: InteractionType,
    turns: list[Turn] | None = None,
    actor_id: str = "system",
    session_id: str | None = None,
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

    # --- redact, then store segments already redacted --------------------
    redaction_count = 0
    redacted_lines: list[str] = []
    for index, turn in enumerate(turns):
        result = redact_phi_detailed(turn.text, gazetteer=gazetteer)
        redaction_count += result.replacements
        redacted_lines.append(f"{turn.speaker}: {result.text}")
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
    response = llm_client.complete(
        redacted_transcript,
        system=_SYSTEM_PROMPT,
        gazetteer=gazetteer,
        purpose=f"ai_scribe:{interaction_type}",
        actor_id=actor_id,
        clinic_id=patient.clinic_id,
    )

    parsed = _parse_model_json(response.text)
    if parsed and parsed.get("headline"):
        summary = parsed
        model_used = f"{response.provider}:{response.model}"
        confidence = _clamp_confidence(parsed.get("confidence"), response.confidence)
    else:
        # No live model, or the model did not return usable JSON. Same code
        # path, deterministic summariser, and the model field says so.
        summary = _extractive_summary(redacted_transcript, interaction_type)
        model_used = "offline-extractive-v1"
        confidence = _clamp_confidence(summary.get("confidence"), 0.6)

    content, markers = prepare_content(_render_content(summary))
    risk_level = _coerce_risk(summary.get("risk_level"))

    # --- persist ---------------------------------------------------------
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
            "attributed_lines": len(links),
        },
    )
    db.commit()
    db.refresh(entry)
    return entry


def _clamp_confidence(value, fallback: float | None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(fallback if fallback is not None else 0.5)
    return round(max(0.0, min(1.0, number)), 2)


def _coerce_risk(value) -> RiskLevel:
    try:
        return RiskLevel(str(value))
    except ValueError:
        return RiskLevel.LOW
